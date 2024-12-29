#!/usr/bin/env python
# -*- coding: utf-8 -*-
import struct
'''
@author: Wei Zhang, Murtaza Safdari
@date: 2018-01-05
This module is used to communicate with control_interface module on FPGA via Ethernet
Edited 2023-03-31
'''

## write config_reg
# @param[in] Addr Address of the configuration register 0-31
# @param[in] Data write into the configuration register 0-65535, [15:0]
def write_config_reg(ss, Addr, Data):
    data = 0x00200000 + (Addr << 16) + Data
    ss.sendall(struct.pack('I',data)[::-1])

## read config_reg
# @param[in] Addr Address of the configuration register 0-31
# return 32bit data
def read_config_reg(ss, Addr):
    data = 0x80200000 + (Addr << 16)
    ss.sendall(struct.pack('I', data)[::-1])
    #print("HERE0")
    #print(struct.unpack('I', ss.recv(4)[::-1])[0])
    #print("HERE1")
    return struct.unpack('I', ss.recv(4)[::-1])[0]

## write pulse_reg
# @param[in] Data write into the pulse register 0-65535
def write_pulse_reg(ss, Data):
    data = 0x000b0000 + Data
    ss.sendall(struct.pack('I',data)[::-1])

## read status_reg
# @param[in] Addr Address of the configuration register 0-10
def read_status_reg(ss, Addr):
    data = 0x80000000 + (Addr << 16)
    ss.sendall(struct.pack('I',data)[::-1])
    return struct.unpack('I', ss.recv(4)[::-1])[0]

## write memeoy
# @param[in] Addr write address of memeoy 0-65535
# @param[in] Data write into memory data 0-65535
def write_memory(ss, Addr, Data):
    data = 0x00110000 + (0x0000ffff & Addr)             #memory address LSB register
    ss.sendall(struct.pack('I',data)[::-1])
    data = 0x00120000 + ((0xffff0000 & Addr) >> 16)     #memory address MSB register
    ss.sendall(struct.pack('I',data)[::-1])
    data = 0x00130000 + (0x0000ffff & Data)             #memory Data LSB register
    ss.sendall(struct.pack('I',data)[::-1])
    data = 0x00140000 + ((0xffff0000 & Data) >> 16)     #memory Data MSB register
    ss.sendall(struct.pack('I',data)[::-1])

## read memory
# @param[in] Cnt read data counts 0-65535
# @param[in] Addr start address of read memory 0-65535
def read_memory(ss, Cnt, Addr):
    data = 0x00100000 + Cnt                             #write sMemioCnt
    ss.sendall(struct.pack('I',data)[::-1])
    data = 0x00110000 + (0x0000ffff & Addr)             #write memory address LSB register
    ss.sendall(struct.pack('I',data)[::-1])
    data = 0x00120000 + ((0xffff0000 & Addr) >> 16)     #write memory address MSB register
    ss.sendall(struct.pack('I',data)[::-1])
    data = 0x80140000                                   #read Cnt 32bit memory words
    ss.sendall(struct.pack('I',data)[::-1])
    for i in range(Cnt):
        print(hex(struct.unpack('I', ss.recv(4)[::-1])[0]))

# TODO Read all the lines and once and refactor into 32 bit words (4 bytes) afterwords
## read_data_fifo
# @param[in] Cnt read data counts 0-65535
def read_data_fifo(ss, Cnt):
    data = 0x00190000 + (Cnt -1)                        #write sDataFifoHigh address = 25
    ss.sendall(struct.pack('I', data)[::-1])
    mem_data = []
    fail_counter = 0
    max_allowed_fails = 15
    for i in range(Cnt):
        try:
            mem_data += [struct.unpack('I', ss.recv(4)[::-1])[0]]
        except struct.error:
            fail_counter = fail_counter + 1
            RuntimeWarning(f"Not enough data in buffer to unpack... This is fail #{fail_counter}/{max_allowed_fails} allowed")
        if(fail_counter>max_allowed_fails):
            RuntimeWarning(f"Breaking with {len(mem_data)}/{Cnt} lines read!")
            break
    return mem_data

#--------------------------------------------------------------------------#
# Following Info is current for commit https://github.com/CMS-ETROC/ETROC2TestFirmware/commit/e40cb281b88d957c9dea2cbba983f1004d6ffce2
#--------------------------------------------------------------------------#
# Pulse Register:
# Reg[15:0] = {4'bxxxx,start_phase_detect,stop_DAQ_pulse,start_DAQ_pulse,start_hist_counter,resumePulse,clear_ws_trig_block_pulse,clrError,initPulse,errInjPulse,fcStart,fifo_reset,START}
def write_pulse_reg_decoded(ss, key=""):
    pulse_registers ={
        "clear_fifo": 0x0002,
        "fc_signal_start": 0x0004,
        "err_inj": 0x0008,
        "fc_init": 0x0010,
        "reset_counter": 0x0020,
        "clear_ws_block": 0x0040,
        "resume_in_debug": 0x0080,
        "start_hist_counter": 0x0100,
        "start_DAQ": 0x0200,
        "stop_DAQ": 0x0400,
        "start_phase_detect": 0x0800,
    }
    if key not in pulse_registers:
        RuntimeError("Invalid Pulse Register Key given!")
    write_pulse_reg(ss, pulse_registers[key])

#--------------------------------------------------------------------------#
# Config Register:
# Reg 4 : {WR_ADDR[7:0],WR_DATA0[7:0]} //I2C
# Reg 5 : {6'bxxxxxx,MODE[1:0],SL_ADDR[6:0],SL_WR} //I2C
# Reg 6 : {8'bxxxxxxxx, WR_DATA1[7:0]} //I2C
# Reg 7 : {6'bxxxxxx,delayTrigCh[3:0],3'bxxx,dis_descr_raw_data,dis_regular_filler, inject_SEU} //trigbit delay or not
# Reg 8 : {trigSelMask[3:0],enhenceData,enableL1Trig,L1Delay[9:0]}
# Reg 9 : {4'bxxxx, initAddressLast[11:0]}
# Reg 10 : {prescale_factor,initAddressFirst[11:0]}
# Reg 11 : {duration[15:0]} \ Reg 12 : {errorMask[7:0],trigDataSize[1:0],period,1'bx,inputCmd[3:0]}
# Reg 13 : {5'bxxxxx, data_delay[5:0],dataRate[1:0],LED_Pages[2:0],status_Pages[1:0]}
# Reg 14 : {auto_prescale,fixed_time_filler,4'bxxxx,falling_edge,manual_mode,sample_event,simple_handshake,add_ethernet_filler,debug_mode,dumping_mode,notGTXPolarity,notGTX,enableAutoSync}
# Reg 15 : {global_trig_delay[4:0],global_trig,trig_or_logic,triple_trig,en_ws_trig,ws_trig_stop_delay[2:0],enableCh[3:0]}
def write_config_reg_decoded(ss, key="", val=None, prescale_factor=2048):
    config_registers ={
        "counter_duration": 7,
        "triggerbit_delay": 8,
        "register_9": 9,
        "register_10": 10,
        "register_11": 11,
        "register_12": 12,
        "timestamp": 13,
        "polarity": 14,
        "active_channel": 15,
    }
    valid_prescale_factors = {
        2048: 0b00,
        4096: 0b01,
        8192: 0b10,
        16384: 0b11,
    }
    if key not in config_registers:
        RuntimeError("Invalid Config Register Key given!")
    if val==None:
        RuntimeError("No Val given to write to Config Register!")
    if(config_registers[key])==10:
        if prescale_factor not in valid_prescale_factors:
            raise RuntimeError("You did not choose a valid prescale factor")
        prescale_bitmask = valid_prescale_factors[prescale_factor]
        mod_val = ((prescale_bitmask & 0b11) << 12) + (val & 0xfff)
        write_config_reg(ss, 10, mod_val)
    else:
        write_config_reg(ss, config_registers[key], val)