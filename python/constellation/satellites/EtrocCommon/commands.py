"""
SPDX-FileCopyrightText: 2025 DESY and the Constellation authors
SPDX-License-Identifier: EUPL-1.2

KC705 register commands shared by EtrocTransmitter and EtrocWaveform.

The class expects the satellite to hold `connection_socket` and the cached
register values `timestamp` (Reg13), `counter_duration` (Reg7),
`fc_delays` (Reg4), `data_delays_01` (Reg5), `data_delays_23` (Reg6),
`polarity` (Reg14), `active_channel` (Reg15) and `fast_command_memo`.

All commands are allowed in ORBIT only: in RUN the TCP stream is busy with
data-FIFO reads, and a register transaction in between would corrupt both.

Payloads are lists, one element per argument (as MissionControl sends them):
from a Python controller call `set_ledpage([2])`, `set_data_phase_channel_delay([5, 1])`.
"""

from typing import Any

from constellation.core.commandmanager import cscp_requestable
from constellation.core.protocol.cscp1 import SatelliteState

from . import cmd_interpret, fast_command

ORBIT = [SatelliteState.ORBIT]


class KC705RegisterCommands:
    """CSCP commands to read and tune KC705 configuration registers.

    Also the register writes of the launch path, shared with reconfigure.
    """

    # Config registers written at launch from the satellite attributes of the same name, in this order
    LAUNCH_REGISTERS = (
        "active_channel", "timestamp", "triggerbit_delay", "polarity",
        "counter_duration", "fc_delays", "data_delays_01", "data_delays_23",
    )
    PRESCALE_FACTORS = (2048, 4096, 8192, 16384)

    @cscp_requestable(ORBIT)
    def get_config_register(self, reg: int) -> tuple[str, Any, dict]:
        """Read one KC705 configuration register (0-31) as a 16-bit binary string."""
        return "FPGA is Ready", format(cmd_interpret.read_config_reg(self.connection_socket, reg), "016b"), {}

    @cscp_requestable(ORBIT)
    def get_status_register(self, reg: int) -> tuple[str, Any, dict]:
        """Read one KC705 status word (0-10) of the page selected by Reg13[1:0]."""
        return "FPGA is Ready", format(cmd_interpret.read_status_reg(self.connection_socket, reg), "016b"), {}

    @cscp_requestable(ORBIT)
    def set_data_phase_delay(self, delay: int) -> tuple[str, Any, dict]:
        """Set the data delay field Reg13[12:7] (0-63)."""
        data_delay = max(0, min(delay, 63))
        mask = ~(0x3F << 7)
        self.timestamp = (self.timestamp & mask) | (data_delay << 7)
        cmd_interpret.write_config_reg_decoded(self.connection_socket, "timestamp", self.timestamp)
        return "FPGA Reg 13 Set, Data Delay Set", f"0x{cmd_interpret.read_config_reg(self.connection_socket, 13):04x}", {}

    @cscp_requestable(ORBIT)
    def set_data_phase_channel_delay(self, delay: int, channel: int) -> tuple[str, Any, dict]:
        """Set the per-channel data delay (0-39) in Reg5 (ch 0/1) or Reg6 (ch 2/3). Payload: [delay, channel]."""
        data_delay = max(0, min(delay, 39))
        channel = max(0, min(channel, 3))
        shift = 0 if channel in (0, 2) else 8
        mask = ~(0x3F << shift)
        if channel < 2:
            self.data_delays_01 = (self.data_delays_01 & mask) | (data_delay << shift)
            cmd_interpret.write_config_reg_decoded(self.connection_socket, "data_delays_01", self.data_delays_01)
            return "FPGA Reg 5 Set, Data Delay Set", f"0x{cmd_interpret.read_config_reg(self.connection_socket, 5):04x}", {}
        self.data_delays_23 = (self.data_delays_23 & mask) | (data_delay << shift)
        cmd_interpret.write_config_reg_decoded(self.connection_socket, "data_delays_23", self.data_delays_23)
        return "FPGA Reg 6 Set, Data Delay Set", f"0x{cmd_interpret.read_config_reg(self.connection_socket, 6):04x}", {}

    @cscp_requestable(ORBIT)
    def set_fc_phase_delay(self, delay: int) -> tuple[str, Any, dict]:
        """Set the FC delay field Reg7[15:10] (0-63). Reg7 is not a counter window on SEU2025."""
        fc_delay = max(0, min(delay, 63))
        mask = ~(0x3F << 10)
        self.counter_duration = (self.counter_duration & mask) | (fc_delay << 10)
        cmd_interpret.write_config_reg_decoded(self.connection_socket, "counter_duration", self.counter_duration)
        return "FPGA Reg 7 Set, FC Phase Delay Set", f"0x{cmd_interpret.read_config_reg(self.connection_socket, 7):04x}", {}

    @cscp_requestable(ORBIT)
    def set_fc_phase_channel_delay(self, delay: int, channel: int) -> tuple[str, Any, dict]:
        """Set the per-channel FC delay (0-31): 4 LSBs in Reg4, MSB in Reg5. Payload: [delay, channel]."""
        fc_delay = max(0, min(delay, 31))
        channel = max(0, min(channel, 3))

        shift_fc = 4 * channel
        mask_fc = ~(0xF << shift_fc)
        self.fc_delays = (self.fc_delays & mask_fc) | ((fc_delay & 0xF) << shift_fc)
        cmd_interpret.write_config_reg_decoded(self.connection_socket, "fc_delays", self.fc_delays)

        msb = (fc_delay >> 4) & 0x1
        shift_msb = {0: 6, 1: 7, 2: 14, 3: 15}[channel]
        mask_msb = ~(1 << shift_msb)
        self.data_delays_01 = (self.data_delays_01 & mask_msb) | (msb << shift_msb)
        cmd_interpret.write_config_reg_decoded(self.connection_socket, "data_delays_01", self.data_delays_01)

        return (
            "FPGA Reg 4 and 5 Set, FC Phase Delay Set",
            [
                f"0x{cmd_interpret.read_config_reg(self.connection_socket, 4):04x}",
                f"0x{cmd_interpret.read_config_reg(self.connection_socket, 5):04x}",
            ],
            {},
        )

    @cscp_requestable(ORBIT)
    def set_fc_bit_delay(self, delay: int) -> tuple[str, Any, dict]:
        """Set the FC bit delay field Reg14[13:10] (0-15)."""
        bit_delay = max(0, min(delay, 15))
        mask = ~(0xF << 10)
        self.polarity = (self.polarity & mask) | (bit_delay << 10)
        cmd_interpret.write_config_reg_decoded(self.connection_socket, "polarity", self.polarity)
        return "FPGA Reg 14 Set, FC Bit Delay Set", f"0x{cmd_interpret.read_config_reg(self.connection_socket, 14):04x}", {}

    @cscp_requestable(ORBIT)
    def set_ledpage(self, page: int) -> tuple[str, Any, dict]:
        """Set LED_Pages Reg13[4:2] (0-5). Pages 0-3 also select the channel shown by the counters."""
        ledpage = max(0, min(page, 5))
        mask = ~(0x7 << 2)
        self.timestamp = (self.timestamp & mask) | (ledpage << 2)
        cmd_interpret.write_config_reg_decoded(self.connection_socket, "timestamp", self.timestamp)
        return "FPGA Reg 13 Set, Led Page Set", format(cmd_interpret.read_config_reg(self.connection_socket, 13), "016b"), {}

    @cscp_requestable(ORBIT)
    def set_active_channel(self, mask: int) -> tuple[str, Any, dict]:
        """Write Reg15 (enableCh[3:0] and trigger options)."""
        self.active_channel = mask
        cmd_interpret.write_config_reg_decoded(self.connection_socket, "active_channel", self.active_channel)
        return "FPGA Reg 15 Set, active_channel Set", format(cmd_interpret.read_config_reg(self.connection_socket, 15), "016b"), {}

    @cscp_requestable(ORBIT)
    def set_fast_command_memo(self, memo: str) -> tuple[str, Any, dict]:
        """Program the FC memory from a memo string, e.g. "Start QInj L1A Triggerbit BCR"."""
        self._check_memo(memo)
        self.fast_command_memo = memo
        self.configure_memo_FC()
        return "Fast Command Configured", self.fast_command_memo, {}

    def _check_memo(self, memo: str) -> None:
        """Validate a memo against the satellite's grammar (EtrocTransmitter grammar by default)."""
        fast_command.parse_memo(memo)

    # ------------------------------------------------------------- launch path
    def _write_registers(self, names) -> None:
        """Write the named config registers from the satellite attributes of the same name."""
        for name in names:
            cmd_interpret.write_config_reg_decoded(self.connection_socket, name, getattr(self, name))

    def _write_prescale(self) -> None:
        """Reg10 prescale bits (the FC memory writes carry them as well)."""
        cmd_interpret.write_config_reg_decoded(self.connection_socket, "register_10", 0x000, self.prescale_factor)

    def _validate_kc705_settings(self, values: dict[str, Any]) -> None:
        """Check prescale_factor and fast_command_memo if present (initialize and reconfigure)."""
        if "prescale_factor" in values and values["prescale_factor"] not in self.PRESCALE_FACTORS:
            raise ValueError(
                f"Prescale factor must be one of {list(self.PRESCALE_FACTORS)}, {values['prescale_factor']} not supported"
            )
        if "fast_command_memo" in values:
            self._check_memo(values["fast_command_memo"])

    def _reconfigure_kc705(self, plan: dict[str, Any]) -> list[str]:
        """Apply reconfigured keys the way do_launching does; registers are read back. Returns the changes."""
        for key, value in plan.items():
            setattr(self, key, value)
        registers = [name for name in self.LAUNCH_REGISTERS if name in plan]
        self._write_registers(registers)
        changes = []
        for name in registers:
            reg = cmd_interpret.CONFIG_REGISTERS[name]
            readback = cmd_interpret.read_config_reg(self.connection_socket, reg)
            changes.append(f"{name} = 0x{getattr(self, name):04x} (Reg{reg} read back 0x{readback:04x})")
        if "prescale_factor" in plan:
            self._write_prescale()
            changes.append(f"prescale_factor = {self.prescale_factor}")
        if "fast_command_memo" in plan or "prescale_factor" in plan:
            self.configure_memo_FC()
            changes.append(f"FC memory reprogrammed from '{self.fast_command_memo}'")
        handled = set(registers) | {"prescale_factor", "fast_command_memo"}
        changes += [f"{key} = {value!r}" for key, value in plan.items() if key not in handled]
        return changes
