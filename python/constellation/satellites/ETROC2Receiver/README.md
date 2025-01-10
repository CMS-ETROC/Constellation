---
# SPDX-FileCopyrightText: Murtaza Safdari, 2024 DESY and the Constellation authors
# SPDX-License-Identifier: CC-BY-4.0 OR EUPL-1.2
title: "ETROC2Receiver"
description: "Data Receiver for ETROC2 DAQ with the KC705"
category: "Data Receiver"
---

## Description

Python Satellite for ETROC2 Data Receiving and Saving, DAQ having been done by an ETROC2Classic Satellite.

## Installation Instructions

Constellation requires newer python versions, it is recommended to use Python > 3.12. 

Here's some simple instructions for pyenv on Ubuntu: https://medium.com/@aashari/easy-to-follow-guide-of-how-to-install-pyenv-on-ubuntu-a3730af8d7f0 

Here's some the pyenv commands: https://github.com/pyenv/pyenv/blob/master/COMMANDS.md

Here's a useful pyenv plugin: https://github.com/pyenv/pyenv-virtualenv

For Bash:
```bash
sudo apt update
sudo apt install -y make build-essential libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev wget curl llvm libncursesw5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev
curl https://pyenv.run | bash
echo -e 'export PYENV_ROOT="$HOME/.pyenv"\nexport PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.bashrc
echo -e 'eval "$(pyenv init --path)"\neval "$(pyenv init -)"' >> ~/.bashrc
exec "$SHELL"
pyenv install --list
pyenv install 3.13.1
git clone https://github.com/pyenv/pyenv-virtualenv.git $(pyenv root)/plugins/pyenv-virtualenv
echo 'eval "$(pyenv virtualenv-init -)"' >> ~/.bashrc
pyenv virtualenv 3.13.1 my-3.13.1
pyenv activate my-3.13.1
pyenv deactivate
```

## Parameters

### User specified parameters

| Parameter | Description | Type | Default Value |
|-----------|-------------|------|---------------|
| `_output_path` | Directory where the data will be stored | String | `data` |
| `translate` | Should the data be translated into `.nem`? (This means no binary data will be saved) | Int | `1` |
| `skip_fillers` | Should the filler lines be skipped in the translated files? | Int | `0` |
| `compressed_binary` | Should the binary files be saved in compressed format `.bin`? (otherwise text file `.dat`) | Int | `1` |
| `flush_interval` | How long to wait before flushing the files during running? | Float | `10.0` |
| `_file_name_pattern` | File name structure, must accomodate `run_identifier` and `date` | String | `run_{run_identifier}_{date}.`+extension |
| `frame_trailers` | Dict specifying Frame Trailer pattern for each channel | Dict | `{0:0x17f0f,1:0x17f0f,2:0x17f0f,3:0x17f0f}` |

### Fixed specified parameters

| Parameter | Description | Value |
|-----------|-------------|-------|
| `fixed_patterns` | Various fixed patterns used during translation | {"clk2_filler": 0x553,"fifo_filler": 0x556, "time_filler": 0x559,"event_header": 0xc3a3c3a,"firmware_key": 0x1,"event_trailer": 0xb,"frame_header": 0x3c5c,"frame_data": 0x1} |
| `fixed_pattern_sizes` | The sizes of each fixed pattern from MSB used during translation | {"clk2_filler": 12,"fifo_filler": 12,"time_filler": 12,"event_header": 28,"firmware_key": 4,"event_trailer": 6,"frame_header": 18,"frame_trailer": 18,"frame_data": 1} |
| `buffer_shifts` | The size of the leftover ETROC2 frame word after removing the MSB 40 bits (1 complete word) from the running buffer | {1:24,2:16,3:8,4:0} |
| `file_size_limit` | The max size of a single output file | 20MB for `.bin`, 50000 lines for `.nem` or `.dat` |
| `file_size_limit` | The max size of a single output file | 20MB for `.bin`, 50000 lines for `.nem` or `.dat` |

## Run Instructions

This satellite does not have any run-time or orbit-time funcitons, it simply starts fetching from the data queue and writing to files, either directly as binary data or after simple translation into nem files.
