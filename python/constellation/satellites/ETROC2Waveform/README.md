---
# SPDX-FileCopyrightText: Murtaza Safdari, 2024 DESY and the Constellation authors
# SPDX-License-Identifier: CC-BY-4.0 OR EUPL-1.2
title: "ETROC2Classic"
description: "Producer Satellite for ETROC2 DAQ with the KC705"
category: "Data Sender"
---

## Description

Python Satellite for ETROC2 DAQ done with the KC705 FPGA via ethernet.
This satellite is a data sender, and must be paired with a data receiver to save and/or process the data.
Please see i2c_gui for I2C with ETROC2.

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

| Parameter | Description | Type | Default Value |
|-----------|-------------|------|---------------|
| `hostname` | Hostname for FPGA Connection (Ethernet) | String | `192.168.2.3` |
| `port` | Port for FPGA Connection (Ethernet) | Int | `1024` |
| `firmware` | Firmware version key | String | `0001` |
| `polarity` | FPGA Config Register 14 | Int | `0x4023` |
| `timestamp` | FPGA Config Register 13 | Int | `0x0000` |
| `active_channel` | FPGA Config Register 15 | Int | `0x0001` |
| `counter_duration` | FPGA Config Register 7 | Int | `0x0000` |
| `triggerbit_delay` | FPGA Config Register 8 | Int | `0x1800` |
| `prescale_factor` | Prescale factor for DAQ, one of [2048, 4096, 8192, 16384] | Int | `2048` |
| `num_fifo_read` | Number of lines to read from the FPGA over ethernet in one chunk | Int | `65536` |
| `clear_fifo` | Should we clear the FPGA FIFOs everytime we configure (launch/reconfig)? | Int | `1` |
| `reset_counter` | Should we reset the FPGA event builder counter on Run Start | Int | `1` |
| `fast_command_memo` | How do we configure the FPGA fast command memo? | String | `Start Triggerbit` |

<!---## Metrics

| Metric | Description | Value Type | Metric Type | Interval |
|--------|-------------|------------|-------------|----------|
| `BRIGHTNESS` | Brightness | Integer | `LAST_VALUE` | 10s |
-->
## Run Instructions

The `INIT` state doesn't connect the Python socket to the FPGA, this only happens in the `ORBIT` state.
The `ORBIT` state also has all the FPGA config registers set via the launch action, which uses the parameters from the config (`.toml`) file.

The FPGA can be reconfigured with a new file in the `ORBIT` state, but the `hostname` and `port` cannot be changed.

## Custom Commands

| Command | Description | Arguments | Return Value | Allowed States |
|---------|-------------|-----------|--------------|----------------|
| `get_config_register` | Get value of a config register from the FPGA | Register Number | Integer | `ORBIT` |
| `get_status_register` | Get value of a status register from the FPGA | Register Number | Integer | `ORBIT` |
