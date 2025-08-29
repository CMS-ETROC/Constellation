import time
import struct
from typing import Any
import numpy as np
from datetime import datetime, timezone, timedelta
from threading import Lock
import threading
import json

from constellation.core.configuration import Configuration
from constellation.core.datasender import DataSender
from constellation.core.commandmanager import cscp_requestable
from constellation.core.cscp import CSCPMessage

from tamalero.ReadoutBoard import ReadoutBoard
from tamalero.ETROC import ETROC
from tamalero.FIFO import FIFO
from tamalero.utils import get_kcu
from tamalero.LPGBT import LPGBT
from tamalero.DataFrame import DataFrame

class ETROC_SMU(DataSender):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.kcu = None
        self.rb = None
        self.etroc_chips = []
        self.etroc_configs = []

        self.fifo = None
        self.df = DataFrame()

        self._acquisition_lock = Lock()
        self._stop_requested = False
        self.hit_counter = 0
        self.trigger_count = 0
        self.baseline_storage = {}

    def do_initializing(self, config: Configuration) -> str:
        self.log.info("Initializing ETROC DAQ System...")

        ## KCU config
        self.kcu_ip = config["kcu_ip"]
        self.rb_id = config.setdefault("readout_board_id", 0)
        self.rb_config = config.setdefault("readout_board_config", "default")
        
        ##ETROC config
        self.etroc_i2c_addresses = config.setdefault("etroc_i2c_addresses", [0x60, 0x61, 0x62, 0x63])
        self.etroc_i2c_channel = config.setdefault("etroc_i2c_channel", 1)
        self.etroc_elinks_map = config.setdefault("etroc_elinks_map", {'0': [0, 4, 8, 12]})

        self.th_offset = config.setdefault("threshold_offset", 50)
        self.charge_fc = config.setdefault("charge_fc", 30)
        self.qinj_count = config.setdefault("qinj_count", 100)
        self.pixel_row = config.setdefault("pixel_row", 1) 
        self.pixel_col = config.setdefault("pixel_col", 2)

        self.trigger_enable_mask = config.setdefault("trigger_enable_mask", 0x1)
        self.trigger_data_size = config.setdefault("trigger_data_size", 1)
        self.trigger_delay_sel = config.setdefault("trigger_delay_sel", 472)

        try:
            self.log.info(f"连接到KCU: {self.kcu_ip}")
            self.kcu = get_kcu(
                self.kcu_ip,
                control_hub=True,
                host='localhost',
                verbose=False
            )
            
            self._test_kcu_connection()
            
            self.rb = ReadoutBoard(
                rb=self.rb_id,
                kcu=self.kcu,
                config=self.rb_config,
                trigger=False,
                verbose=False
            )
            self.log.info(f"RB version: {self.rb.ver}")
            
            self._initialize_etroc_chips()
            
            return f"Successfully initialized {len(self.etroc_chips)} Etroc chips"

        except Exception as e:
            self.log.error(f"Hardware initialization failed: {e}")
            raise RuntimeError(f"Hardware initializetion failed: {e}")
    


    def do_launching(self) -> str:
        self.log.info("Starting baseline scaning and Configuring pixels")

        try:
            self._calibrate_baselines()
            self._configure_etroc_for_cosmic()
            self._configure_trigger_system()
     
            self.fifo = FIFO(self.rb)
            self.fifo.reset()
            self.rb.reset_data_error_count()
            self.rb.enable_etroc_readout()
            self.rb.rerun_bitslip()
            self.fifo.use_etroc_data()

            return f"Syetem launched, {self.pixel_col * self.pixel_row} pixels configured"

        except Exception as e:
            self.log.error(f"System launched failed: {e}")
            raise RuntimeError(f"System launched failed {e}")
        
    def do_starting(self, run_identifier: str) -> str:
        self.log.info(f"Preparing to start run: {run_identifier}")
        self.hit_counter = 0
        self.trigger_count = 0 
        self._stop_requested = False
        
        self.BOR = {
            "run_id": run_identifier,
            "start_time": datetime.now(timezone.utc).isoformat(),
            "config": self.config.get_dict() 
        }
        self.rb.enable_etroc_trigger()
        time.sleep(0.1)
        
        return f"DAQ ready, identifier: {run_identifier}"

    def do_run(self, payload: Any) -> str:
        self.log.info(f"DAQ running for run: {payload}")
        
        stop_sender_evt = threading.Event()
        def qinj_sender():
            self.log.info("Qinj sender thread started.")
            while not stop_sender_evt.is_set():
                try:
                    self.fifo.send_Qinj_only(count=self.qinj_count)
                    time.sleep(0.1)
                except Exception as e:
                    self.log.error(f"Error in qinj_sender thread: {e}")
                    break
            self.log.info("Qinj sender thread stopped.")

        sender_thread = threading.Thread(target=qinj_sender, daemon=True)
        if self.qinj_count > 0: 
            sender_thread.start()

        while not self._state_thread_evt.is_set():
            try:
                raw_data_chunk = self.fifo.read(dispatch=True)
                if raw_data_chunk:
                    packed_data = struct.pack(f'<{len(raw_data_chunk)}I', *raw_data_chunk)
                    self.data_queue.put((packed_data, {"timestamp_ns": time.time_ns()}))
                else:
                    time.sleep(0.01)
            except Exception as e:
                self.log.error(f"Critical error in DAQ loop: {e}", exc_info=True)
                self.fsm.failure(f"DAQ loop error: {e}")
                break
        
        if sender_thread.is_alive():
            stop_sender_evt.set()
            sender_thread.join(timeout=1)
                
        self.log.info("do_run loop has exited.")
        return "Acquisition finished."
    
    def do_stopping(self) -> str:
        self.log.info("Stop cosmic ray detection...")
        self.rb.disable_etroc_trigger()
        self._stop_requested = True
        
        self.EOR = {
            "end_time": datetime.now(timezone.utc).isoformat(),
            "total_hits": self.hit_counter,
            "total_triggers": self.trigger_count
        }
        
        return f"DAQ stopped - total hits: {self.hit_counter}"
    
    def do_landing(self) -> str:
        self.log.info("System cleaning...")
        
        try:
            if self.fifo:
                self.fifo.reset()
            if self.rb:
                self.rb.reset_data_error_count()

            for etroc, chip_name, _ in self.etroc_configs:
                self.log.debug(f"cleaning {chip_name}...")
                etroc.wr_reg("QInjEn", 0, broadcast=True)
                etroc.wr_reg("disDataReadout", 1, broadcast=True)
                time.sleep(0.1)
            
            return "System cleaning done"
            
        except Exception as e:
            self.log.error(f"System cleaning failed: {e}")
            return f"System cleaning failed with error: {e})"
    
    
    def _test_kcu_connection(self):
 
        loopback_val = 0xABCD1234
        self.kcu.write_node("LOOPBACK.LOOPBACK", loopback_val)
        read_val = self.kcu.read_node("LOOPBACK.LOOPBACK").value()
        
        if read_val != loopback_val:
            raise RuntimeError(f"KCU loopback failed: write in 0x{loopback_val:X}, read back 0x{read_val:X}")
        
        self.log.info("KCU connection passed")
    
    def _initialize_etroc_chips(self):
       
        self.etroc_chips = []
        chip_names = []
        elinks_map_from_config = self.etroc_elinks_map

        #Constellation require key from dict must string, however, tamalero keys type is int
        self.log.info("Converting elinks map keys from string to int for tamalero")
        try:
            elinks_map_for_tamalero = {int(k) : v for k, v in elinks_map_from_config.items()}
        except (ValueError, TypeError) as e:
            raise RuntimeError(f"Failed to convert elinks map keys to int. Error: {e}")
        
        for i, addr in enumerate(self.etroc_i2c_addresses):
            chip_name = f"Chip{i+1}"
            chip_names.append(chip_name)
            
            self.log.info(f"Initializing {chip_name} (I2C: 0x{addr:02X})...")
            
            try:
                etroc = ETROC(
                    self.rb,
                    master='lpgbt',
                    i2c_adr=addr,
                    i2c_channel=self.etroc_i2c_channel,
                    # elinks=self.etroc_elinks_map,
                    elinks = elinks_map_for_tamalero,
                    strict=False,
                    verbose=False
                )
                self.etroc_chips.append(etroc)
                
                if etroc.is_connected():
                    self.log.info(f"✓ {chip_name} connection succeed")
                else:
                    self.log.error(f"✗ {chip_name} connection failed")
                    
            except Exception as e:
                self.log.error(f"Initialization {chip_name} failed: {e}")
                self.etroc_chips.append(None)
 
        self.etroc_configs = []
        for i, (etroc, chip_name) in enumerate(zip(self.etroc_chips, chip_names)):
            if etroc is not None:
                pixels = [(row, col) for row in range(self.pixel_row) 
                         for col in range(self.pixel_col)]
                self.etroc_configs.append((etroc, chip_name, pixels))
    
    def _calibrate_baselines(self):
        
        self.log.info(f"Starting scan {self.pixel_row * self.pixel_col} pixel's baseline...")
        
        self.baseline_storage = {}
        failed_pixels = {}
        
        for etroc, chip_name, test_pixels in self.etroc_configs:
            self.baseline_storage[chip_name] = {}
            failed_pixels[chip_name] = []
            
            self.log.info(f"Calibrating {chip_name}...")
            
            for pixel_row, pixel_col in test_pixels:
                try:
                    baseline, _ = etroc.auto_threshold_scan(
                        row=pixel_row,
                        col=pixel_col,
                        broadcast=False,
                        offset='auto',
                        use=False,
                        verbose=False 
                    )
                    self.baseline_storage[chip_name][(pixel_row, pixel_col)] = baseline
                    
                except Exception as e:
                    self.log.warning(f"Pixel ({pixel_row},{pixel_col}) calibrated failed: {e}")
                    failed_pixels[chip_name].append((pixel_row, pixel_col))
        
        total_failed = sum(len(failed_list) for failed_list in failed_pixels.values())
        if total_failed > 0:
            self.log.warning(f" {total_failed} pixels failed in calibration")
        else:
            self.log.info("All pixel calibrated")
    
    def _configure_etroc_for_cosmic(self):

        self.log.info("Configuring Etroc and pixelx...")
        
        for _, (etroc, chip_name, all_pixels) in enumerate(self.etroc_configs):
            self.log.info(f"Configuring {chip_name}...")
            etroc.reset()
            time.sleep(0.1)
            etroc.wr_reg("singlePort", 1)
            
            etroc.wr_reg("disDataReadout", 1, broadcast=True)
            etroc.wr_reg("QInjEn", 0, broadcast=True)
            etroc.wr_reg("enable_TDC", 0, broadcast=True)
            etroc.wr_reg("disTrigPath", 1, broadcast=True)
            etroc.wr_reg("workMode", 0, broadcast=True)
            etroc.wr_reg('triggerGranularity', 1)
            time.sleep(0.1)
            
            for pixel_row, pixel_col in all_pixels:
                etroc.wr_reg("workMode", 0, row=pixel_row, col=pixel_col, broadcast=False)
                etroc.wr_reg("enable_TDC", 1, row=pixel_row, col=pixel_col, broadcast=False)
                etroc.wr_reg("disDataReadout", 0, row=pixel_row, col=pixel_col, broadcast=False)
                etroc.wr_reg("disTrigPath", 0, row=pixel_row, col=pixel_col, broadcast=False)  
            
                baseline = self.baseline_storage[chip_name][(pixel_row, pixel_col)]
                applied_dac = baseline + self.th_offset
                etroc.wr_reg('DAC', applied_dac, row=pixel_row, col=pixel_col, broadcast=False)
                etroc.wr_reg("QSel", self.charge_fc - 1, row=pixel_row, col=pixel_col, broadcast=False)
                etroc.wr_reg("QInjEn", 1, row=pixel_row, col=pixel_col, broadcast=False)
                
                time.sleep(0.01)  
    
    def _configure_trigger_system(self):
   
        self.log.info("Configure trigger system...")
        self.rb.kcu.write_node(f"READOUT_BOARD_{self.rb.rb}.TRIG_ENABLE_MASK", self.trigger_enable_mask)
        self.rb.kcu.write_node(f"READOUT_BOARD_{self.rb.rb}.TRIG_DATA_SIZE", self.trigger_data_size)
        self.rb.kcu.write_node(f"READOUT_BOARD_{self.rb.rb}.TRIG_DLY_SEL", self.trigger_delay_sel)
        
        # Check E-link
        for elink in [0, 4, 8, 12]:
            locked = self.rb.etroc_locked(elink, slave=False)
            self.log.info(f"E-link {elink} locked status: {locked}")
            if not locked:
                self.log.warning(f"E-link {elink} unlock, try to unlock...")
                self.rb.rerun_bitslip()
                time.sleep(0.5)
                locked = self.rb.etroc_locked(elink, slave=False)
                if not locked:
                    raise RuntimeError(f"E-link {elink} lock failed")
    
    def fail_gracefully(self) -> str:
        if self.rb:
            try:
                self.rb.reset_data_error_count()
            except:
                pass
        
        if self.etroc_chips:
            for etroc in self.etroc_chips:
                if etroc:
                    try:
                        etroc.wr_reg("QInjEn", 0, broadcast=True)
                        etroc.wr_reg("disDataReadout", 1, broadcast=True)
                    except:
                        pass
        
        return "System landed safely"

    
