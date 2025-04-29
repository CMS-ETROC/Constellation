import i2c_gui2
import logging
import sys
import time
import pandas as pd
import plotly.express as px

class i2c_connection():
    _chips = None

    def __init__(self, port, chip_addresses, ws_addresses, chip_names, clock = 100):
        self.chip_addresses = chip_addresses
        self.ws_addresses = ws_addresses
        self.chip_names = chip_names

        ## Logger
        log_level = 30
        logging.basicConfig(format='%(asctime)s - %(levelname)s:%(name)s:%(message)s', stream=sys.stdout, force=False, level=log_level)
        # logger = logging.getLogger("Script_Logger")
        self.chip_logger = logging.getLogger("Chip_Logger")
        self.conn = i2c_gui2.USB_ISS_Helper(port, clock, dummy_connect = False)
        # logger.setLevel(log_level)
        self.chip_logger.setLevel(log_level)
    
    def __del__(self):
        del self.conn
    
    def get_chip_i2c_connection(self, chip_address, ws_address=None):
        if self._chips is None:
            self._chips = {}

        if chip_address not in self._chips:
            self._chips[chip_address] = i2c_gui2.ETROC2_Chip(chip_address, ws_address, self.conn, self.chip_logger)

        return self._chips[chip_address]
    def start_ws_sampling(self):
        for chip_address,ws_address in zip(self.chip_addresses,self.ws_addresses):
            chip: i2c_gui2.ETROC2_Chip = self.get_chip_i2c_connection(chip_address, ws_address)

            chip.read_register("Waveform Sampler", "Config", "regOut1F")
            chip["Waveform Sampler", "Config", "regOut1F"] = 0x22
            chip.write_register("Waveform Sampler", "Config", "regOut1F")
            chip["Waveform Sampler", "Config", "regOut1F"] = 0x0b
            chip.write_register("Waveform Sampler", "Config", "regOut1F")

            # self.ws_decoded_register_write("clk_gen_rstn", "0", chip=chip)                  # 0: reset clock generation
            # self.ws_decoded_register_write("sel1", "0", chip=chip)                          # 0: Bypass mode, 1: VGA mode
            chip.read_decoded_value("Waveform Sampler", "Config", 'mem_rstn')
            chip.set_decoded_value("Waveform Sampler", "Config", 'mem_rstn', 0)
            chip.write_decoded_value("Waveform Sampler", "Config", 'mem_rstn')
            chip.set_decoded_value("Waveform Sampler", "Config", 'mem_rstn', 1)
            chip.write_decoded_value("Waveform Sampler", "Config", 'mem_rstn')

            chip.read_decoded_value("Waveform Sampler", "Config", 'DDT')
            chip.set_decoded_value("Waveform Sampler", "Config", 'DDT', 0)        # Time Skew Calibration set to 0
            chip.write_decoded_value("Waveform Sampler", "Config", 'DDT')

            chip.read_register("Waveform Sampler", "Config", "regOut0D")
            chip.set_decoded_value("Waveform Sampler", "Config", 'CTRL', 2)       # CTRL default = 0x10 for regOut0D
            chip.write_decoded_value("Waveform Sampler", "Config", 'CTRL')
            chip.set_decoded_value("Waveform Sampler", "Config", 'comp_cali', 0)       # Comparator calibration should be off
            chip.write_decoded_value("Waveform Sampler", "Config", 'comp_cali')

    def stop_ws_sampling(self):
        for chip_address,ws_address in zip(self.chip_addresses,self.ws_addresses):
            chip: i2c_gui2.ETROC2_Chip = self.get_chip_i2c_connection(chip_address, ws_address)
            chip.read_register("Waveform Sampler", "Config", "regOut1F")
            chip["Waveform Sampler", "Config", "regOut1F"] = 0x09
            chip.write_register("Waveform Sampler", "Config", "regOut1F")
    
    def read_chip_ws(self, chip_address, ws_address):
        chip: i2c_gui2.ETROC2_Chip = self.get_chip_i2c_connection(chip_address, ws_address)
        i2c_controller: i2c_gui2.I2C_Connection_Helper = chip._i2c_connection
        chip.read_decoded_value("Waveform Sampler", "Config", 'rd_en_I2C')
        chip.set_decoded_value("Waveform Sampler", "Config", 'rd_en_I2C', 1)        #active high
        chip.write_decoded_value("Waveform Sampler", "Config", 'rd_en_I2C')
        
        max_steps = 1024  # Size of the data buffer inside the WS
        base_data = []
        coeff = 0.05/5*8.5  # This number comes from the example script in the manual
        time_coeff = 1/2.56  # 2.56 GHz WS frequency
        addr_regs = [0x00, 0x00]  # regOut1C and regOut1D
        for address in range(max_steps):
            addr_regs[0] = ((address & 0b11) << 6)          # 0x1C
            addr_regs[1] = ((address & 0b1111111100) >> 2)  # 0x1D
            i2c_controller.write_device_memory(ws_address, 0x1C, addr_regs, 8)
            tmp_data = i2c_controller.read_device_memory(ws_address, 0x20, 2, 8)
            data = hex((tmp_data[0] >> 2) + (tmp_data[1] << 6))
            binary_data = bin(int(data, 0))[2:].zfill(14)  # because dout is 14 bits long
            Dout_S1 = int('0b'+binary_data[1:7], 0)
            Dout_S2 = int(binary_data[ 7]) * 24 + \
                        int(binary_data[ 8]) * 16 + \
                        int(binary_data[ 9]) * 10 + \
                        int(binary_data[10]) *  6 + \
                        int(binary_data[11]) *  4 + \
                        int(binary_data[12]) *  2 + \
                        int(binary_data[13])
            base_data.append(
                {
                    "Data Address": address,
                    "Data": int(data, 0),
                    "Raw Data": bin(int(data, 0))[2:].zfill(14),
                    "pointer": int(binary_data[0]),
                    "Dout_S1": Dout_S1,
                    "Dout_S2": Dout_S2,
                    "Dout": Dout_S1 - coeff * Dout_S2,
                }
            )
        # print("Done with Address Loop")
        df = pd.DataFrame(base_data)
        df_length = len(df)
        channels = 8
        df_per_ch : list[pd.DataFrame] = []
        for ch in range(channels):
            df_per_ch += [df.iloc[int(ch * df_length/channels):int((ch + 1) * df_length/channels)].copy()]
            df_per_ch[ch].reset_index(inplace = True, drop = True)
        pointer_idx = df_per_ch[-1]["pointer"].loc[df_per_ch[-1]["pointer"] != 0].index  # TODO: Maybe add a search of the pointer in any channel, not just the last one
        if len(pointer_idx) != 0:  # If pointer found, reorder the data
            pointer_idx = pointer_idx[0]
            new_idx = list(set(range(len(df_per_ch[-1]))).difference(range(pointer_idx+1))) + list(range(pointer_idx+1))
            for ch in range(channels):
                df_per_ch[ch] = df_per_ch[ch].iloc[new_idx].reset_index(drop = True)  # Fix indexes after reordering
        # interleave the channels
        for ch in range(channels):
            df_per_ch[ch]["Time Index"] = df_per_ch[ch].index * channels + (channels - 1 - ch)  # Flip the order of the channels in the interleave...
            df_per_ch[ch]["Channel"] = ch + 1
        # Actually put it all together in one dataframe and sort the data correctly
        df = pd.concat(df_per_ch)
        df["Time [ns]"] = df["Time Index"] * time_coeff
        df.set_index('Time Index', inplace=True)
        df.sort_index(inplace=True)

        chip.read_decoded_value("Waveform Sampler", "Config", 'rd_en_I2C')
        chip.set_decoded_value("Waveform Sampler", "Config", 'rd_en_I2C', 0)        #active high
        chip.write_decoded_value("Waveform Sampler", "Config", 'rd_en_I2C')

        return df