Constellation + Tamalero ETROC DAQ System
# Constellation + Tamalero ETROC DAQ System

The architecture employs a satellite-based design with two primary components:

- ETROC_SMU: Data producer satellite that controls hardware and streams raw data from the FIFO buffer
- ETROC2Receiver: Data consumer satellite that receives network data and writes it to disk in binary format
- Both Satelites source code under path: your_home_path/Constellation/python/constellation/satellites

## Prerequisites
Ensure the following requirements are met before installation:
- **Python**: Version 3.11 or newer
- **Hardware Access**: ETROC readout hardware with network connectivity
- **Repository Access**: Both constellation and tamalero source code repositories
- **Dependencies**: ipbus for uHAL support (required by tamalero)

## Installation
### 1. Create Python Virtual Environment

```bash
# In your main workspace directory
python3 -m venv .venv
source .venv/bin/activate
```
Your terminal prompt should display `(.venv)` when the environment is active.

### 2. Install Constellation Framework
Follow the official Constellation installation guide for complete setup:
https://constellation.pages.desy.de/operator_guide/get_started/install_from_source.html

Install both packages in editable mode for development:

```bash (included in the official installation guide)
# Install Constellation (navigate to constellation repository root)
cd path/to/your/constellation/
pip install --no-build-isolation -e .

# Install Tamalero (navigate to tamalero repository root)  
cd path/to/your/tamalero/
pip install -e .
```
## System Configuration

System parameters are managed through a TOML configuration file. Create `etroc_daq_config.toml` in your constellation repository root:

```toml
# ETROC_SMU satellite configuration (data producer)
[satellites.ETROC_SMU.ETROC_DAQ]
kcu_ip = "192.168.0.10"
readout_board_id = 0
readout_board_config = "default"

# Set qinj_count > 0 for charge injection tests
# Set qinj_count = 0 for passive acquisition (e.g., cosmic ray detection)
qinj_count = 100

# ETROC parameters
threshold_offset = 50
charge_fc = 30
pixel_row = 1
pixel_col = 2
trigger_enable_mask = 1
trigger_data_size = 1
trigger_delay_sel = 472

# ETROC2Receiver satellite configuration (data consumer)
[satellites.ETROC2Receiver.Receiver]
# Critical: Must match the producer satellite name exactly
_data_transmitters = ["ETROC_SMU.ETROC_DAQ"]
# Output configuration
output_path = "/path/to/your/data/output"
# Binary output mode settings
translate = false
compressed_binary = true
skip_fillers = false

# File naming pattern
file_name_pattern = "{run_identifier}/file_{date}.bin"
```

## Running the DAQ System

### 1. Start Satellite Processes

Open two terminal windows and activate the virtual environment in both:

**Terminal 1 - Data Producer:**
```bash
source .venv/bin/activate
SatelliteETROC_SMU -n ETROC_DAQ -g smu_test
```

**Terminal 2 - Data Consumer:**
```bash
source .venv/bin/activate
SatelliteETROC2Receiver -n ETROC_Receiver -g smu_test
```
### 2. Launch Mission Control
Open a third terminal and start the controller:

```bash
source .venv/bin/activate
# Navigate to MissionControl build directory (adjust path as needed)
./build/cxx/controllers/MissionControl/MissionControl -g smu_test
```

### 3. Control Sequence
When the MissionControl UI appears, follow this sequence:

1. **Configure**: Select your `.toml` configuration file
2. **Initialize**: Initialize all satellites with the loaded configuration  
3. **Launch**: Prepare satellites for data acquisition
4. **Start**: Begin data acquisition run
5. **Stop**: End the current run
6. **Land**: Return satellites to safe state
7. **Shutdown**: Terminate all processes (or use Ctrl+C)

## System Operation Notes
- **Group Name**: All satellites must use the same group name (`smu_test` in examples)
- **Network Discovery**: Satellites automatically discover each other via CHIRP protocol
- **Data Format**: Output files are written in compressed binary format
- **Run Management**: Each run creates timestamped output files in the specified directory structure
