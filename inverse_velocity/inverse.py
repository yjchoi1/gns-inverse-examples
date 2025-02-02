import time
import sys
import os
import numpy as np
import toml
import json
import glob
import argparse
import torch.utils.checkpoint
from forward import rollout_with_checkpointing
from utils import make_animation
from utils import visualize_final_deposits
from utils import visualize_velocity_profile
from utils import To_Torch_Model_Param

sys.path.append('gns')
from gns import reading_utils
from gns import data_loader
from gns import train


parser = argparse.ArgumentParser()
parser.add_argument('--input_path', default="config.toml", type=str, help="Input file path")
args = parser.parse_args()

# Open config file
inputs = toml.load(args.input_path)

path = inputs["path"]

# inputs for optimizer
optimizer_type = inputs["optimization"]["type"]
niteration = inputs["optimization"]["niteration"]
inverse_timestep_range = inputs["optimization"]["inverse_timestep_range"]
checkpoint_interval = inputs["optimization"]["checkpoint_interval"]
lr = inputs["optimization"]["lr"]
initial_velocities = inputs["optimization"]["initial_velocities"]

# inputs for ground truth
ground_truth_npz = inputs["ground_truth"]["ground_truth_npz"]
ground_truth_mpm_inputfile = inputs["ground_truth"]['ground_truth_mpm_inputfile']

# inputs for forward simulator
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
noise_std = 6.7e-4  # hyperparameter used to train GNS.
NUM_PARTICLE_TYPES = 9
dt_mpm = inputs["forward_simulator"]['dt_mpm']
model_path = inputs["forward_simulator"]['model_path']
model_file = inputs["forward_simulator"]['model_file']
simulator_metadata_path = inputs["forward_simulator"]['simulator_metadata_path']
simulator_metadata_file = inputs["forward_simulator"]['simulator_metadata_file']

# inputs for output setup
output_dir = inputs["output"]['output_dir']
save_step = inputs["output"]['save_step']

# resume
resume = inputs["resume"]['resume']
resume_iteration = inputs["resume"]['iteration']


# Load simulator
metadata = reading_utils.read_metadata(simulator_metadata_path, "rollout", file_name=simulator_metadata_file)
simulator = train._get_simulator(metadata, noise_std, noise_std, device)
if os.path.exists(model_path + model_file):
    simulator.load(model_path + model_file)
else:
    raise Exception(f"Model does not exist at {model_path + model_file}")
simulator.to(device)
simulator.eval()

# Get ground truth particle position at the inversion timestep
mpm_trajectory = [item for _, item in np.load(f"{path}/{ground_truth_npz}", allow_pickle=True).items()]
target_final_positions = torch.tensor(
    mpm_trajectory[0][0][inverse_timestep_range[0]: inverse_timestep_range[1]], device=device)

# Get ground truth velocities for each particle group.
f = open(f"{path}/{ground_truth_mpm_inputfile}")
mpm_inputs = json.load(f)
velocity_constraints = mpm_inputs["mesh"]["boundary_conditions"]["particles_velocity_constraints"]
# Initialize an empty NumPy array with the shape (max_pset_id+1, 2)
max_pset_id = max(item['pset_id'] for item in velocity_constraints)
ground_truth_vels = np.zeros((max_pset_id + 1, 2))
# Fill in the NumPy array with velocity values from data
for constraint in velocity_constraints:
    pset_id = constraint['pset_id']
    dir = constraint['dir']
    velocity = constraint['velocity']
    ground_truth_vels[pset_id, dir] = velocity

# Get initial position (i.e., p_0) for each particle group
particle_files = sorted(glob.glob(f"{path}/particles*.txt"))
particle_groups = []
particle_group_idx_ranges = []
count = 0
for filename in particle_files:
    particle_group = torch.tensor(np.loadtxt(filename, skiprows=1))
    particle_groups.append(particle_group)
    particle_group_idx_range = np.arange(count, count+len(particle_group))
    count = count+len(particle_group)
    particle_group_idx_ranges.append(particle_group_idx_range)
initial_position = torch.concat(particle_groups).to(device)

# Initialize initial velocity (i.e., dot{p}_0)
initial_velocity_x = torch.tensor(
    initial_velocities, requires_grad=True, device=device)
initial_velocity_x_model = To_Torch_Model_Param(initial_velocity_x)

# Set up the optimizer
if optimizer_type == "lbfgs":
    optimizer = torch.optim.LBFGS(initial_velocity_x_model.parameters(), lr=lr, max_iter=4)
elif optimizer_type == "adam":
    optimizer = torch.optim.Adam(initial_velocity_x_model.parameters(), lr=lr)
elif optimizer_type == "sgd":
    optimizer = torch.optim.SGD(initial_velocity_x_model.parameters(), lr=lr)
else:
    raise ValueError("Check `optimizer_type`")

# Set output folder
if not os.path.exists(f"{output_dir}"):
    os.makedirs(f"{output_dir}")
# Save the current config to the output dir
with open(f"{output_dir}/config.json", "w") as outfile:
    json.dump(inputs, outfile, indent=4)

# Resume
if resume:
    print(f"Resume from the previous state: iteration{resume_iteration}")
    checkpoint = torch.load(f"{output_dir}/optimizer_state-{resume_iteration}.pt")
    if optimizer_type == "adam" or optimizer_type == "sgd":
        start_iteration = checkpoint["iteration"]
    else:
        start_iteration = checkpoint["lbfgs_iteration"]
        closure_count = checkpoint["iteration"]
    initial_velocity_x_model.load_state_dict(checkpoint['velocity_x_state_dict'])
    optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
else:
    start_iteration = 0
    if optimizer_type == "lbfgs":
        closure_count = 0

initial_velocity_x = initial_velocity_x_model.current_params
initial_velocity_y = torch.full((len(initial_velocity_x), 1), 0).to(device)

# Start optimization iteration
if optimizer_type == "adam" or optimizer_type == "sgd":
    for iteration in range(start_iteration+1, niteration):
        start = time.time()
        optimizer.zero_grad()  # Clear previous gradients

        # Load data containing X0, and get necessary features.
        # First, get particle type and material property.
        dinit = data_loader.TrajectoriesDataset(path=f"{path}/{ground_truth_npz}")
        for example_i, features in enumerate(dinit):  # only one item exists in `dint`. No need `for` loop
            # Obtain features
            if len(features) < 3:
                raise NotImplementedError("Data should include material feature")
            particle_type = features[1].to(device)
            material_property = features[2].to(device)
            n_particles_per_example = torch.tensor([int(features[3])], dtype=torch.int32).to(device)

        # Next, make [p0, p1, ..., p5] using current initial velocity, assuming that velocity is the same for 5 timesteps
        initial_velocity = torch.hstack((initial_velocity_x, initial_velocity_y))
        initial_pos_seq_all_group = []
        for i, particle_group_idx_range in enumerate(particle_group_idx_ranges):
            initial_pos_seq_each_group = [
                initial_position[particle_group_idx_range] + initial_velocity[i] * dt_mpm * t for t in range(6)]
            initial_pos_seq_all_group.append(torch.stack(initial_pos_seq_each_group))
        initial_positions = torch.concat(initial_pos_seq_all_group, axis=1).to(device).permute(1, 0, 2).to(torch.float32).contiguous()

        # print(f"Initial velocities: {initial_velocity.detach().cpu().numpy()}")

        predicted_positions = rollout_with_checkpointing(
            simulator=simulator,
            initial_positions=initial_positions,
            particle_types=particle_type,
            material_property=material_property,
            n_particles_per_example=n_particles_per_example,
            nsteps=inverse_timestep_range[1] - initial_positions.shape[1] + 1, # exclude initial positions (x0) which we already have
            checkpoint_interval=checkpoint_interval
        )

        inversion_positions = predicted_positions[inverse_timestep_range[0]:inverse_timestep_range[1]]

        loss = torch.mean((inversion_positions - target_final_positions) ** 2)
        print("Backpropagate...")
        loss.backward()

        # Visualize current prediction
        print(f"iteration {iteration - 1}, Loss {loss.item():.8f}")
        print(f"Initial vel: {initial_velocity.detach().cpu().numpy()}")
        visualize_final_deposits(predicted_positions=predicted_positions,
                                 target_positions=target_final_positions,
                                 metadata=metadata,
                                 write_path=f"{output_dir}/final_deposit-{iteration - 1}.png")
        visualize_velocity_profile(predicted_velocities=initial_velocity,
                                   target_velocities=ground_truth_vels,
                                   write_path=f"{output_dir}/vel_profile-{iteration - 1}.png")

        # Perform optimization step
        optimizer.step()

        end = time.time()
        time_for_iteration = end - start

        # Save and report optimization status
        if iteration % save_step == 0:

            # Make animation at the last iteration
            if iteration == niteration - 1:
                print(f"Rendering animation at {iteration}...")
                positions_np = np.concatenate(
                    (initial_positions.permute(1, 0, 2).detach().cpu().numpy(),
                     predicted_positions.detach().cpu().numpy())
                )
                make_animation(positions=positions_np,
                               boundaries=metadata["bounds"],
                               output=f"{output_dir}/animation-{iteration}.gif",
                               timestep_stride=5)

            # Save history
            current_history = {
                "iteration": iteration,
                "lr": optimizer.state_dict()["param_groups"][0]["lr"],
                "initial_velocity_x": initial_velocity_x.detach().cpu().numpy(),
                "loss": loss.item()
            }

            # Save optimizer state
            torch.save({
                'iteration': iteration,
                'time_spent': time_for_iteration,
                'position_state_dict': {
                    "target_positions": mpm_trajectory[0][0],
                    "inversion_positions": predicted_positions.clone().detach().cpu().numpy()
                },
                'velocity_x_state_dict': To_Torch_Model_Param(initial_velocity_x).state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'loss': loss,
            }, f"{output_dir}/optimizer_state-{iteration}.pt")

elif optimizer_type == "lbfgs":

    save_dict = {}
    def closure():
        global closure_count
        start = time.time()

        closure_count += 1

        print(f"Number of closure calls: {closure_count}, iteration {iteration}")
        optimizer.zero_grad()  # Clear previous gradients

        # Load data containing X0, and get necessary features.
        # First, get particle type and material property.
        dinit = data_loader.TrajectoriesDataset(path=f"{path}/{ground_truth_npz}")
        for example_i, features in enumerate(dinit):  # only one item exists in `dint`. No need `for` loop
            # Obtain features
            if len(features) < 3:
                raise NotImplementedError("Data should include material feature")
            particle_type = features[1].to(device)
            material_property = features[2].to(device)
            n_particles_per_example = torch.tensor([int(features[3])], dtype=torch.int32).to(device)

        # Next, make [p0, p1, ..., p5] using current initial velocity, assuming that velocity is the same for 5 timesteps
        initial_velocity = torch.hstack((initial_velocity_x, initial_velocity_y))
        initial_pos_seq_all_group = []
        for i, particle_group_idx_range in enumerate(particle_group_idx_ranges):
            initial_pos_seq_each_group = [
                initial_position[particle_group_idx_range] + initial_velocity[i] * dt_mpm * t for t in range(6)]
            initial_pos_seq_all_group.append(torch.stack(initial_pos_seq_each_group))
        initial_positions = torch.concat(initial_pos_seq_all_group, axis=1).to(device).permute(1, 0, 2).to(
            torch.float32).contiguous()

        # Prediction
        predicted_positions = rollout_with_checkpointing(
            simulator=simulator,
            initial_positions=initial_positions,
            particle_types=particle_type,
            material_property=material_property,
            n_particles_per_example=n_particles_per_example,
            nsteps=inverse_timestep_range[1] - initial_positions.shape[1] + 1,  # exclude initial positions (x0) which we already have
            # exclude initial positions (x0) which we already have
            checkpoint_interval=checkpoint_interval
        )

        # Target
        inversion_positions = predicted_positions[inverse_timestep_range[0]:inverse_timestep_range[1]]

        # Compute loss
        loss = torch.mean((inversion_positions - target_final_positions) ** 2)
        print("Backpropagate...")
        loss.backward()

        # Visualize current prediction
        print(f"Number of optim step calls {closure_count - 1}, Loss {loss.item():.8f}")
        print(f"Initial vel: {initial_velocity.detach().cpu().numpy()}")
        visualize_final_deposits(predicted_positions=predicted_positions,
                                 target_positions=target_final_positions,
                                 metadata=metadata,
                                 write_path=f"{output_dir}/final_deposit-{closure_count - 1}.png")
        visualize_velocity_profile(predicted_velocities=initial_velocity,
                                   target_velocities=ground_truth_vels,
                                   write_path=f"{output_dir}/vel_profile-{closure_count - 1}.png")

        end = time.time()
        time_for_iteration = end - start

        # Save and report optimization status
        if closure_count % save_step == 0:

            # Make animation at the last iteration
            if closure_count == niteration - 1:
                print(f"Rendering animation at {closure_count}...")
                positions_np = np.concatenate(
                    (initial_positions.permute(1, 0, 2).detach().cpu().numpy(),
                     predicted_positions.detach().cpu().numpy())
                )
                make_animation(positions=positions_np,
                               boundaries=metadata["bounds"],
                               output=f"{output_dir}/animation-{closure_count}.gif",
                               timestep_stride=5)

            # Save optimizer state per closure
            save_dict['iteration'] = closure_count
            save_dict['time_spent'] = time_for_iteration
            save_dict['position_state_dict'] = {
                "target_positions": mpm_trajectory[0][0],
                "inversion_positions": predicted_positions.clone().detach().cpu().numpy()
            }
            save_dict['velocity_x_state_dict'] = To_Torch_Model_Param(initial_velocity_x).state_dict()
            save_dict['optimizer_state_dict'] = optimizer.state_dict()
            save_dict['loss'] = loss
            torch.save(save_dict, f"{output_dir}/optimizer_state-{closure_count}.pt")

        return loss

    for iteration in range(start_iteration+1, niteration):
        optimizer.step(closure)
        print(f"LBFGS iteration {iteration}, closure {closure_count}----------------------------------------")
        save_dict["lbfgs_iteration"] = iteration
        torch.save(save_dict, f"{output_dir}/optimizer_state_i{iteration}_c{closure_count}.pt")

