import pandas as pd
import numpy as np

import logging
import time, sys

from datetime import datetime
from tqdm import tqdm

# ANSI colors
GREEN = '\033[32m'
RED   = "\033[31m"
RESET = '\033[0m'

# =======================================================================
# i2c_gui2 registers TRACE (8) and DETAILED_TRACE (5) at import with an addLoggingLevel that raises
# when the name already exists, and Constellation already defines logging.TRACE (5) and
# ConstellationLogger.trace. Removing those for the duration of the import (the former workaround)
# broke every thread that logged at TRACE meanwhile: a controller's heartbeat checker or a
# satellite's heartbeat sender died with "'ConstellationLogger' object has no attribute 'trace'".
# Instead, i2c_gui2.functions is loaded first with an addLoggingLevel that only adds what is missing,
# so nothing that exists is removed or renamed, and the package import then picks it up.
# =======================================================================
import importlib.util
import os


def _add_missing_logging_level(levelName: str, levelNum: int, methodName: str | None = None) -> None:
    """i2c_gui2's addLoggingLevel without the raise: existing names, numbers and methods win."""
    methodName = methodName or levelName.lower()
    if not hasattr(logging, levelName):
        setattr(logging, levelName, levelNum)
        if logging.getLevelName(levelNum).startswith("Level "):
            logging.addLevelName(levelNum, levelName)
    level = getattr(logging, levelName)

    def log_for_level(self, message, *args, **kwargs):
        if self.isEnabledFor(level):
            self._log(level, message, args, **kwargs)

    def log_to_root(message, *args, **kwargs):
        logging.log(level, message, *args, **kwargs)

    # on the base class, so loggers of any class made before or after this import have it
    if not hasattr(logging.getLoggerClass(), methodName) and not hasattr(logging.Logger, methodName):
        setattr(logging.Logger, methodName, log_for_level)
    if not hasattr(logging, methodName):
        setattr(logging, methodName, log_to_root)


def _import_i2c_gui2():
    if "i2c_gui2" not in sys.modules and "i2c_gui2.functions" not in sys.modules:
        spec = importlib.util.find_spec("i2c_gui2")  # locates the package without running it
        locations = list(spec.submodule_search_locations or []) if spec else []
        path = os.path.join(locations[0], "functions.py") if locations else ""
        fspec = importlib.util.spec_from_file_location("i2c_gui2.functions", path) if os.path.isfile(path) else None
        if fspec is not None and fspec.loader is not None:
            functions = importlib.util.module_from_spec(fspec)
            fspec.loader.exec_module(functions)
            functions.addLoggingLevel = _add_missing_logging_level
            sys.modules["i2c_gui2.functions"] = functions
    import i2c_gui2 as module

    if "i2c_gui2.functions" in sys.modules and not hasattr(module, "functions"):
        module.functions = sys.modules["i2c_gui2.functions"]
    return module


i2c_gui2 = _import_i2c_gui2()
# =======================================================================

class i2c_connection:
    def __init__(self, port: str, chip_addresses: list, ws_addresses: list, chip_names: list, clock: int = 100,
                 logger: logging.Logger | None = None):
        self.chip_addresses = chip_addresses
        self.ws_addresses = ws_addresses
        self.chip_names = chip_names

        # 1. Instance-Specific Caches
        # Keeps 'chips' bound to this specific object, not shared globally across the class
        self._chips = {}
        self.BL_df = {addr: pd.DataFrame() for addr in chip_addresses}

        # 2. Simplified Delay Tracking
        self.fc_delays = {addr: {'clk': 1, 'data': 1} for addr in chip_addresses}

        # 3. Logger: a child of the caller's logger (a satellite's records then reach its console
        #    and CMDP handlers, once), or a standalone stdout logger for notebooks and scripts
        if logger is not None:
            self.logger = logger.getChild("I2C")
            self.logger.propagate = True
        else:
            self.logger = logging.getLogger("I2C_Manager")
            self.logger.setLevel(logging.INFO)
            self.logger.propagate = False  # Prevents it from touching the global root

        if logger is None and not self.logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)

        # 4. Build the dummy logger for the chip object
        self.i2c_logger = logging.getLogger("Silent_Chip")
        self.i2c_logger.setLevel(logging.CRITICAL)
        self.i2c_logger.propagate = False
        if not self.i2c_logger.handlers:
            self.i2c_logger.addHandler(logging.NullHandler())

        self.conn = i2c_gui2.USB_ISS_Helper(port, clock, dummy_connect=False)

        # =======================================================================
        # THE SNIPER: Kill the newly spawned I2C_Log
        # =======================================================================
        rogue_logger = logging.getLogger("I2C_Log")
        rogue_logger.setLevel(logging.CRITICAL) # Ignore INFO/DEBUG
        rogue_logger.propagate = False          # Don't send to Constellation
        rogue_logger.handlers.clear()           # Rip off any hardcoded prints
        # =======================================================================

    # ==========================================
    # THE GATEKEEPER
    # ==========================================
    def _resolve_chip(self, addr: int) -> i2c_gui2.ETROC2_Chip:
        if addr not in self._chips:
            try:
                idx = self.chip_addresses.index(addr)
                ws_addr = self.ws_addresses[idx]
            except ValueError:
                ws_addr = None

            # Use self.logger here instead of self.chip_logger
            self._chips[addr] = i2c_gui2.ETROC2_Chip(addr, ws_addr, self.conn, self.i2c_logger)

        return self._chips[addr]

    def close(self) -> None:
        """Release the USB-ISS port (USB_ISS_Helper only closes it in __del__)."""
        iss = getattr(self.conn, "_iss", None) if getattr(self, "conn", None) is not None else None
        if iss is not None:
            iss.close()
            self.conn._iss = None
        self._chips = {}
        self.conn = None

    def get_chip(self, addr: int) -> i2c_gui2.ETROC2_Chip:
        """Public access to the cached chip object for `addr`."""
        return self._resolve_chip(addr)

    def config_TDC_window_ranges_in_memory(self, addr: int, window_dict: dict = None):
        chip = self._resolve_chip(addr)

        window = window_dict or {
            "upperTOATrig": 0x3ff, "lowerTOATrig": 0x000,
            "upperTOTTrig": 0x1ff, "lowerTOTTrig": 0x000,
            "upperCalTrig": 0x3ff, "lowerCalTrig": 0x000,
            "upperTOA": 0x3ff,     "lowerTOA": 0x000,
            "upperTOT": 0x1ff,     "lowerTOT": 0x000,
            "upperCal": 0x3ff,     "lowerCal": 0x000,
        }
        for key, value in window.items():
            chip.set_decoded_value("ETROC2", "Pixel Config", key, value)

    # ==========================================
    # LEVEL 1: SINGLE-CHIP HARDWARE METHODS
    # ==========================================
    def disable_all_pixels(self, addr: int, power_mode: str = 'high'):
        """
        Disables all pixels for exactly ONE chip.
        No loops, no external logic. Just direct hardware control.
        """
        chip = self._resolve_chip(addr)
        chip.row = 0
        chip.col = 0

        # Fast dictionary lookup replaces if/elif chains
        power_map = {'high': 0b000, '010': 0b010, '101': 0b101, 'low': 0b111}
        ibsel = power_map.get(power_mode, 0b000)

        chip.read_all_block("ETROC2", "Pixel Config")

        pixel_config = {
            "disDataReadout": 1, "QInjEn": 0, "disTrigPath": 1,
            "upperTOATrig": 0x000, "lowerTOATrig": 0x000,
            "upperTOTTrig": 0x1ff, "lowerTOTTrig": 0x1ff,
            "upperCalTrig": 0x3ff, "lowerCalTrig": 0x3ff,
            "upperTOA": 0x000, "lowerTOA": 0x000,
            "upperTOT": 0x1ff, "lowerTOT": 0x1ff,
            "upperCal": 0x3ff, "lowerCal": 0x3ff,
            "enable_TDC": 0,
            "IBSel": ibsel,
            "Bypass_THCal": 1,
            "TH_offset": 0x3f,
            "DAC": 0x3ff,
        }

        for key, value in pixel_config.items():
            chip.set_decoded_value("ETROC2", "Pixel Config", key, value)

        chip.broadcast = True
        chip.write_all_block("ETROC2", "Pixel Config")
        chip.broadcast = False

        self.logger.info(f'  SUCCESS - Disable all 256 pixels of ({hex(addr)})')

    # ==========================================
    # LEVEL 2: BATCH OPERATIONS / ORCHESTRATORS
    # ==========================================
    def batch_disable_all_chips(self, power_mode: str = 'high'):
        """
        A thin wrapper that loops over the single-chip methods.
        The user calls this when they want system-wide action.
        """
        for addr in self.chip_addresses:
            self.disable_all_pixels(addr, power_mode)

    # ==========================================
    # LEVEL 1: CORE PHYSICS (Single Pixel)
    # ==========================================
    def auto_cal_single_pixel(self, addr: int, row: int, col: int) -> list:
        """
        Executes the hardware auto-calibration sequence for a single pixel.
        RETURNS: A dictionary of the exact state for that pixel.
        """
        chip = self._resolve_chip(addr)
        chip.row, chip.col = row, col

        # 1. Hardware Setup
        chip.read_all_block("ETROC2", "Pixel Config")
        setup_vals = {"enable_TDC": 0, "CLKEn_THCal": 1, "BufEn_THCal": 1, "Bypass_THCal": 0}
        for key, val in setup_vals.items():
            chip.set_decoded_value("ETROC2", "Pixel Config", key, val)
        chip.write_all_block("ETROC2", "Pixel Config")

        # 2. Pulse Reset and Start
        for val in [0, 1]:
            chip.set_decoded_value("ETROC2", "Pixel Config", "RSTn_THCal", val)
            chip.write_decoded_value("ETROC2", "Pixel Config", "RSTn_THCal")

        for val in [1, 0]:
            chip.set_decoded_value("ETROC2", "Pixel Config", "ScanStart_THCal", val)
            chip.write_decoded_value("ETROC2", "Pixel Config", "ScanStart_THCal")

        # 3. Safe Polling (Time-bound)
        start_poll = time.monotonic()
        while True:
            chip.read_decoded_value("ETROC2", "Pixel Status", "ScanDone")
            if chip.get_decoded_value("ETROC2", "Pixel Status", "ScanDone") == 1:
                break
            if time.monotonic() - start_poll > 1.0:
                self.logger.error(f"  TIMEOUT: ScanDone not set for ({row},{col}) on {hex(addr)}")
                break
            time.sleep(0.01)

        # 4. Fetch Data
        chip.read_all_block("ETROC2", "Pixel Status")
        bl_nw_data = [
            chip.get_decoded_value("ETROC2", "Pixel Status", "BL"),
            chip.get_decoded_value("ETROC2", "Pixel Status", "NW"),
            datetime.now(),
        ]

        # 5. Cleanup
        cleanup_vals = {"enable_TDC": 0, "CLKEn_THCal": 0, "BufEn_THCal": 0, "Bypass_THCal": 1, "DAC": 0x3ff}
        for key, val in cleanup_vals.items():
            chip.set_decoded_value("ETROC2", "Pixel Config", key, val)
        chip.write_all_block("ETROC2", "Pixel Config")

        return bl_nw_data

    def config_single_pixel(self, addr: int, row: int, col: int, Qsel: int = 0x1e,
                            QInjEn: bool = False, Bypass_THCal: bool = True,
                            power_mode: str = "high", manual_DAC: int = 0x3ff, rfsel: int | None = None):
        """
        Sets all parameters for a specific pixel in one fast transaction.
        rfsel (preamp feedback resistor, PixCfg0[6:5], 0 = largest R = highest gain) is written only when given;
        otherwise the pixel keeps the value it has (chip default 2).
        """
        chip = self._resolve_chip(addr)
        chip.row, chip.col = row, col

        power_map = {'high': 0b000, '010': 0b010, '101': 0b101, 'low': 0b111}
        ibsel_val = power_map.get(power_mode, 0b000)

        settings = {
            'disDataReadout': 0, 'QInjEn': 1 if QInjEn else 0, 'disTrigPath': 0,
            'L1Adelay': 0x01f5, 'Bypass_THCal': 1 if Bypass_THCal else 0,
            'TH_offset': 0x14, 'QSel': Qsel, 'DAC': manual_DAC,
            'enable_TDC': 1, 'IBSel': ibsel_val,
        }
        if rfsel is not None:
            settings['RFSel'] = rfsel

        chip.read_all_block("ETROC2", "Pixel Config")

        # Write standard TDC windows (Assume this helper is also adapted to Level 1)
        self.config_TDC_window_ranges_in_memory(addr)

        for key, value in settings.items():
            chip.set_decoded_value("ETROC2", "Pixel Config", key, value)

        chip.write_all_block("ETROC2", "Pixel Config")

    def read_pixel_config(self, addr: int, row: int, col: int, fields) -> dict:
        """Read one pixel's Pixel Config block from the chip; returns {field: value} for `fields` (read only)."""
        chip = self._resolve_chip(addr)
        chip.row, chip.col = row, col
        chip.read_all_block("ETROC2", "Pixel Config")
        return {f: int(chip.get_decoded_value("ETROC2", "Pixel Config", f)) for f in fields}

    # ==========================================
    # LEVEL 2: BATCH OPERATIONS / ORCHESTRATORS
    # ==========================================
    def batch_read_pixel_config(self, pixel_list: list[tuple], fields) -> dict:
        """read_pixel_config of every pixel on every chip: {hex(chip address): {"row,col": {field: value}}}."""
        return {
            hex(addr): {f"{r},{c}": self.read_pixel_config(addr, r, c, fields) for r, c in pixel_list}
            for addr in self.chip_addresses
        }

    def batch_auto_calibrate_chip(self, pixel_list: list[tuple]):
        """
        Orchestrates a full 16x16 calibration scan for ONE chip and saves the DataFrame.
        """
        for addr, chip_name in zip(self.chip_addresses, self.chip_names):
            self.logger.info(f'  Starting auto-calibration for {chip_name} ({hex(addr)})...')
            results = {
                'row': [],
                'col': [],
                'baseline': [],
                'noise_width': [],
                'timestamp': [],
            }

            # We can use your preferred tqdm loop here
            for row, col in tqdm(pixel_list, desc=f"Auto baseline calibration for {hex(addr)}"):
                    # Call Level 1 method and capture the dict
                    pixel_data = self.auto_cal_single_pixel(addr, row, col)

                    results['row'].append(row)
                    results['col'].append(col)
                    results['baseline'].append(pixel_data[0])
                    results['noise_width'].append(pixel_data[1])
                    results['timestamp'].append(pixel_data[2])

            # Build DataFrame instantly from the list of dicts
            bl_nw_df = pd.DataFrame(results)
            bl_nw_df['chip_name'] = chip_name

            # Store in the instance state
            self.BL_df[addr] = bl_nw_df
            self.logger.info(f'  Finished auto-calibration for {chip_name} ({hex(addr)})...')

    def batch_enable_pixels(self, pixel_list: list[tuple], Qsel: int = 0x1e,
                            QInjEn: bool = True, Bypass_THCal: bool = True,
                            power_mode: str = "high", offset: int = None, manual_DAC: int = 0x3ff,
                            rfsel: int | None = None):
        """
        Loops through a provided list of (row, col) tuples and configures them.
        If 'offset' is provided, it automatically grabs the baseline from self.BL_df.
        """
        for addr in self.chip_addresses:
            self.logger.info(f'  Starting pixels configuration for ({hex(addr)})...')
            for row, col in tqdm(pixel_list, desc=f"Configuring Pixels on {hex(addr)}"):

                final_dac = manual_DAC
                # Determine the DAC automatically if offset is requested
                if offset is not None:
                    df = self.BL_df[addr]
                    bl = df.loc[(df['row'] == row) & (df['col'] == col), 'baseline'].values[0]
                    final_dac = bl + offset

                self.config_single_pixel(
                    addr, row, col, Qsel=Qsel, QInjEn=QInjEn,
                    Bypass_THCal=Bypass_THCal, power_mode=power_mode,
                    manual_DAC=final_dac, rfsel=rfsel
                )
            self.logger.info(f'  Finished pixels configuration for ({hex(addr)})...')

    # ==========================================
    # LEVEL 1: PERIPHERALS & UTILITIES
    # ==========================================
    def set_chip_peripherals(self, addr: int):
        """Configures the global peripheral registers for a specific chip."""
        try:
            chip = self._resolve_chip(addr)
            chip.read_all_block("ETROC2", "Peripheral Config")

            settings = {
                "EFuse_Prog": 0x00017f0f,         # Chip ID
                "singlePort": 1,                  # Set data output to right port only
                "serRateLeft": 0b00,              # 320 mbps
                "serRateRight": 0b00,             # 320 mbps
                "onChipL1AConf": 0b00,            # Switches off the onboard L1A
                "PLL_ENABLEPLL": 1,               # Debugging use only
                "chargeInjectionDelay": 0x0a,     # User tunable delay of Qinj pulse
                "triggerGranularity": 0x01,       # Only for trigger bit
                "fcClkDelayEn": self.fc_delays[addr]['clk'],
                "fcDataDelayEn": self.fc_delays[addr]['data']
            }

            for key, val in settings.items():
                chip.set_decoded_value("ETROC2", "Peripheral Config", key, val)

            chip.write_all_block("ETROC2", "Peripheral Config")
            self.logger.info(f"  Peripherals set for {hex(addr)}")

        except Exception as e:
            self.logger.error(f"  Failed to set peripherals on {hex(addr)}: {e}")

    def batch_set_chip_peripherals(self):
        """
        A thin wrapper that loops over the single-chip methods.
        The user calls this when they want system-wide action.
        """
        for addr in self.chip_addresses:
            self.set_chip_peripherals(addr)

    def calibratePLL(self, addr: int):
        """Hardware sequence to reset and lock the PLL."""
        chip = self._resolve_chip(addr)

        # 1. PLL Reset (Toggle 0 -> 1)
        chip.read_decoded_value("ETROC2", "Peripheral Config", 'asyPLLReset')
        for val in [0, 1]:
            chip.set_decoded_value("ETROC2", "Peripheral Config", 'asyPLLReset', val)
            chip.write_decoded_value("ETROC2", "Peripheral Config", 'asyPLLReset')
            time.sleep(0.1)

        # 2. Start Calibration (Toggle 0 -> 1)
        chip.read_decoded_value("ETROC2", "Peripheral Config", 'asyStartCalibration')
        for val in [0, 1]:
            chip.set_decoded_value("ETROC2", "Peripheral Config", 'asyStartCalibration', val)
            chip.write_decoded_value("ETROC2", "Peripheral Config", 'asyStartCalibration')
            time.sleep(0.1)

    def asyAlignFastcommand(self, addr: int):
        """Hardware sequence to align the Fast Command decoder."""
        chip = self._resolve_chip(addr)
        chip.read_decoded_value("ETROC2", "Peripheral Config", 'asyAlignFastcommand')

        # Toggle 1 -> 0
        for val in [1, 0]:
            chip.set_decoded_value("ETROC2", "Peripheral Config", 'asyAlignFastcommand', val)
            chip.write_decoded_value("ETROC2", "Peripheral Config", 'asyAlignFastcommand')
            if val == 1: time.sleep(0.1)

    def asyResetGlobalReadout(self, addr: int):
        """Hardware sequence to reset the global readout block."""
        chip = self._resolve_chip(addr)
        chip.read_decoded_value("ETROC2", "Peripheral Config", 'asyResetGlobalReadout')

        # Toggle 0 -> 1
        for val in [0, 1]:
            chip.set_decoded_value("ETROC2", "Peripheral Config", 'asyResetGlobalReadout', val)
            chip.write_decoded_value("ETROC2", "Peripheral Config", 'asyResetGlobalReadout')
            if val == 0: time.sleep(0.1)

    def batch_PLL_FC_calibration(self):
        """
        A thin wrapper that loops over the single-chip methods.
        The user calls this when they want system-wide action.
        """
        for addr in self.chip_addresses:
            self.calibratePLL(addr)
            self.asyResetGlobalReadout(addr)
            self.asyAlignFastcommand(addr)

    def config_fc_data_delay(self, addr: int, fc_clk_delay: int, fc_data_delay: int):
        """Updates the Fast Command delays in memory and on the chip."""
        if not (0 <= fc_clk_delay <= 1) or not (0 <= fc_data_delay <= 1):
            raise ValueError('Delays must be 0 or 1')

        self.fc_delays[addr]['clk'] = fc_clk_delay
        self.fc_delays[addr]['data'] = fc_data_delay

        chip = self._resolve_chip(addr)
        chip.read_register("ETROC2", "Peripheral Config", "PeriCfg18")
        chip.set_decoded_value("ETROC2", "Peripheral Config", "fcClkDelayEn", fc_clk_delay)
        chip.set_decoded_value("ETROC2", "Peripheral Config", "fcDataDelayEn", fc_data_delay)
        chip.write_register("ETROC2", "Peripheral Config", "PeriCfg18")


    # ==========================================
    # LEVEL 1: DIAGNOSTIC & CHECK METHODS
    # ==========================================
    def pixel_check(self, addr: int) -> bool:
        """
        Verifies the consistency of the PixelID (Row/Col) for the entire 16x16 grid.
        """
        chip = self._resolve_chip(addr)
        pixel_flag_fail = False

        try:
            for row in range(16):
                for col in range(16):
                    chip.row, chip.col = row, col

                    # Read and verify PixelID consistency
                    chip.read_decoded_value("ETROC2", "Pixel Status", 'PixelID')
                    fetched_row = chip.get_decoded_value("ETROC2", "Pixel Status", 'PixelID-Row')
                    fetched_col = chip.get_decoded_value("ETROC2", "Pixel Status", 'PixelID-Col')

                    if row != fetched_row or col != fetched_col:
                        self.logger.error(f"  {hex(addr)}: Pixel ({row}, {col}) failed consistency check! Returned ({fetched_row}, {fetched_col})")
                        pixel_flag_fail = True

            return not pixel_flag_fail

        except Exception as e:
            self.logger.error(f"  Error in pixel_check for {hex(addr)}: {e}")
            return False

    # --------------------------------------------------------------------------
    def basic_peripheral_register_check(self, addr: int) -> bool:
        """
        Performs a bit-flip (XOR 0xFF) test on all 32 peripheral configuration registers
        to verify write/read persistence and consistency.
        """
        chip = self._resolve_chip(addr)
        peri_flag_fail = False

        try:
            # Initial bulk read of the block
            chip.read_all_block("ETROC2", "Peripheral Config")

            for i in range(32):
                reg_name = f"PeriCfg{i}"

                # 1. Fetch original and create bit-flipped version
                original_val = chip["ETROC2", "Peripheral Config", reg_name]
                modified_val = original_val ^ 0xff

                # 2. Write flipped value and verify immediately
                chip["ETROC2", "Peripheral Config", reg_name] = modified_val
                chip.write_register("ETROC2", "Peripheral Config", reg_name)

                # Internal read-back check
                read_1 = chip["ETROC2", "Peripheral Config", reg_name]
                chip.read_register("ETROC2", "Peripheral Config", reg_name)
                read_2 = chip["ETROC2", "Peripheral Config", reg_name]

                # 3. Restore original value and verify
                chip["ETROC2", "Peripheral Config", reg_name] = original_val
                chip.write_register("ETROC2", "Peripheral Config", reg_name)
                restored_val = chip["ETROC2", "Peripheral Config", reg_name]

                # Validation Logic
                if read_1 != read_2 or read_2 != modified_val or restored_val != original_val:
                    self.logger.error(f"  {hex(addr)}: {reg_name} bit-flip check FAILURE")
                    peri_flag_fail = True

            return not peri_flag_fail

        except Exception as e:
            self.logger.error(f"  Error in peripheral_check for {hex(addr)}: {e}")
            return False

    # ==========================================
    # LEVEL 2: BATCH DIAGNOSTICS
    # ==========================================
    def batch_pixel_check(self) -> bool:
        """Runs the PixelID consistency check on all registered chips."""
        for addr in self.chip_addresses:
            success = self.pixel_check(addr)
            status = f"{GREEN}PASSED{RESET}" if success else f"{RED}FAILED{RESET}"
            self.logger.info(f"  System Check: PixelID consistency for {hex(addr)}: {status}")

    def batch_peripheral_check(self) -> bool:
        """Runs the bit-flip register check on all registered chips."""
        for addr in self.chip_addresses:
            success = self.basic_peripheral_register_check(addr)
            status = f"{GREEN}PASSED{RESET}" if success else f"{RED}FAILED{RESET}"
            self.logger.info(f"  System Check: Peripheral registers for {hex(addr)}: {status}")

    def check_invalid_fc(self, addr: int, samples: int = 3, interval: float = 0.3) -> list[int]:
        """
        Reads the invalid FCCount register multiple times to check for decoding stability.
        Returns a list of the counter values found.
        """
        chip = self._resolve_chip(addr)
        invalid_fc_values = []

        for _ in range(samples):
            # Read and fetch the decoded counter
            chip.read_decoded_value("ETROC2", "Peripheral Status", 'invalidFCCount')
            val = chip.get_decoded_value("ETROC2", "Peripheral Status", "invalidFCCount")
            invalid_fc_values.append(val)
            time.sleep(interval)

        return invalid_fc_values

    def batch_check_invalid_fc(self):
        """
        System-wide check of Invalid FC counters for all registered chips.
        """
        self.logger.info("  Checking Invalid FC counters across all chips...")

        for addr in self.chip_addresses:
            fc_values = self.check_invalid_fc(addr)
            self.logger.info(f"  Chip {hex(addr)} Invalid FC Counter: {fc_values}")

    # ==========================================
    # LEVEL 1: WAVEFORM SAMPLER (WS) METHODS
    # ==========================================
    def ws_start_sampling(self, addr: int, sel1: int = 0, sel2: int = 0):
        """Initializes and starts the Waveform Sampler for a single chip.

        regOut1F ends at 0x0b (mem_rstn, en_clk, clk_gen_rstn = 1; rd_en_I2C, sel3 = 0) plus the mode bits
        (ETROC2 manual sec. 6.5): sel1 (bit 7) 1 = VGA amplifier, 0 = bypass; sel2 (bit 6) selects the
        power-on mode (1 = 400 ns after ws_start, 0 = from ws_start until ws_stop, keeping the last 400 ns).
        """
        chip = self._resolve_chip(addr)
        final = 0x0b | ((sel1 & 1) << 7) | ((sel2 & 1) << 6)

        # 1. State Machine Reset & Start
        chip.read_register("Waveform Sampler", "Config", "regOut1F")
        chip["Waveform Sampler", "Config", "regOut1F"] = 0x22
        chip.write_register("Waveform Sampler", "Config", "regOut1F")
        chip["Waveform Sampler", "Config", "regOut1F"] = final
        chip.write_register("Waveform Sampler", "Config", "regOut1F")

        # 2. Memory Reset Toggle
        chip.read_decoded_value("Waveform Sampler", "Config", 'mem_rstn')
        chip.set_decoded_value("Waveform Sampler", "Config", 'mem_rstn', 0)
        chip.write_decoded_value("Waveform Sampler", "Config", 'mem_rstn')
        chip.set_decoded_value("Waveform Sampler", "Config", 'mem_rstn', 1)
        chip.write_decoded_value("Waveform Sampler", "Config", 'mem_rstn')

        # 3. Calibration Disables
        chip.read_decoded_value("Waveform Sampler", "Config", 'DDT')
        chip.set_decoded_value("Waveform Sampler", "Config", 'DDT', 0)        # Disable Time Skew Cal
        chip.write_decoded_value("Waveform Sampler", "Config", 'DDT')

        chip.read_register("Waveform Sampler", "Config", "regOut0D")
        chip.set_decoded_value("Waveform Sampler", "Config", 'CTRL', 2)       # CTRL default = 0x10
        chip.write_decoded_value("Waveform Sampler", "Config", 'CTRL')
        chip.set_decoded_value("Waveform Sampler", "Config", 'comp_cali', 0)  # Disable Comparator Cal
        chip.write_decoded_value("Waveform Sampler", "Config", 'comp_cali')

        self.logger.info(f"  WS Sampling STARTED for {hex(addr)}")

    def ws_stop_sampling(self, addr: int):
        """Stops the Waveform Sampler state machine."""
        chip = self._resolve_chip(addr)
        chip.read_register("Waveform Sampler", "Config", "regOut1F")
        chip["Waveform Sampler", "Config", "regOut1F"] = 0x09
        chip.write_register("Waveform Sampler", "Config", "regOut1F")
        self.logger.info(f"  WS Sampling STOPPED for {hex(addr)}")

    def ws_read_data(self, addr: int) -> pd.DataFrame:
        """
        Reads the 1024-step memory from the Waveform Sampler and parses it instantly.
        """
        # Automatically look up the paired WS address
        idx = self.chip_addresses.index(addr)
        ws_addr = self.ws_addresses[idx]

        if ws_addr is None:
            self.logger.warning(f"  Cannot read WS for {hex(addr)} - No WS address assigned.")
            return pd.DataFrame()

        chip = self._resolve_chip(addr)
        i2c_controller = chip._i2c_connection

        # Enable I2C read mode
        chip.read_decoded_value("Waveform Sampler", "Config", 'rd_en_I2C')
        chip.set_decoded_value("Waveform Sampler", "Config", 'rd_en_I2C', 1)
        chip.write_decoded_value("Waveform Sampler", "Config", 'rd_en_I2C')

        max_steps = 1024
        base_data = []
        coeff = 0.085
        time_coeff = 0.390625
        addr_regs = [0x00, 0x00]

        for address in range(max_steps):
            addr_regs[0] = ((address & 0b11) << 6)
            addr_regs[1] = ((address & 0b1111111100) >> 2)
            i2c_controller.write_device_memory(ws_addr, 0x1C, addr_regs, 8)
            tmp_data = i2c_controller.read_device_memory(ws_addr, 0x20, 2, 8)

            data_int = (tmp_data[0] >> 2) | (tmp_data[1] << 6)
            pointer = (data_int >> 13) & 1
            Dout_S1 = (data_int >> 7) & 0x3F

            Dout_S2 = (((data_int >> 6) & 1) * 24 +
                       ((data_int >> 5) & 1) * 16 +
                       ((data_int >> 4) & 1) * 10 +
                       ((data_int >> 3) & 1) * 6 +
                       ((data_int >> 2) & 1) * 4 +
                       ((data_int >> 1) & 1) * 2 +
                        (data_int & 1))

            base_data.append({
                "Data Address": address, "Data": data_int, "Raw Data": f"{data_int:014b}",
                "pointer": pointer, "Dout_S1": Dout_S1, "Dout_S2": Dout_S2,
                "Dout": Dout_S1 - (coeff * Dout_S2),
            })

        df = pd.DataFrame(base_data)
        channels = 8
        steps_per_ch = len(df) // channels

        df['Channel'] = np.repeat(np.arange(1, channels + 1), steps_per_ch)
        df['Step'] = np.tile(np.arange(steps_per_ch), channels)

        pointer_mask = (df['Channel'] == channels) & (df['pointer'] != 0)
        if pointer_mask.any():
            pointer_step = df.loc[pointer_mask, 'Step'].iloc[0]
            df['Step'] = (df['Step'] - (pointer_step + 1)) % steps_per_ch

        df['Time Index'] = df['Step'] * channels + (channels - df['Channel'])
        df['Time [ns]'] = df['Time Index'] * time_coeff

        df = df.sort_values('Time Index').set_index('Time Index').drop(columns=['Step'])

        # Disable I2C read mode
        chip.set_decoded_value("Waveform Sampler", "Config", 'rd_en_I2C', 0)
        chip.write_decoded_value("Waveform Sampler", "Config", 'rd_en_I2C')

        self.logger.info(f"  WS Data Read COMPLETE for {hex(addr)}")
        return df


    # ==========================================
    # LEVEL 2: BATCH WS OPERATIONS
    # ==========================================
    def batch_ws_start_sampling(self, sel1: int = 0, sel2: int = 0):
        """Starts the Waveform Sampler on all registered chips (sel1 / sel2: see ws_start_sampling)."""
        self.logger.info("Batch starting WS sampling...")
        for addr in self.chip_addresses:
            self.ws_start_sampling(addr, sel1=sel1, sel2=sel2)

    def batch_ws_stop_sampling(self):
        """Stops the Waveform Sampler on all registered chips."""
        self.logger.info("Batch stopping WS sampling...")
        for addr in self.chip_addresses:
            self.ws_stop_sampling(addr)

    def batch_ws_read_data(self) -> dict:
        """
        Reads WS data from all chips.
        Returns a dictionary mapping chip addresses to their DataFrames.
        """
        self.logger.info("Batch reading WS data...")
        ws_results = {}
        for addr, name in zip(self.chip_addresses, self.chip_names):
            ws_results[name] = self.ws_read_data(addr)
        return ws_results