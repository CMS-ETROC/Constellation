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

## read_data_fifo
# @param[in] Cnt read data counts 0-65535
def read_data_fifo(ss, Cnt):
    data = 0x00190000 + (Cnt -1)                        #write sDataFifoHigh address = 25
    # print("before sendall() in read fifo()...")
    ss.sendall(struct.pack('I', data)[::-1])
    # print("after sendall() in read fifo()...")
    mem_data = []
    for i in range(Cnt-1):
        # print(f"inside loop {i} to recv but before first recv this loop...")
        try:
            mem_data += [struct.unpack('I', ss.recv(4)[::-1])[0]]
        except struct.error:
            print("not enough data in buffer to unpack...")
            return mem_data
        # print("Data fetched in this iteration: ", mem_data[-1])
    try:
        mem_data += [struct.unpack('I', ss.recv(4)[::-1])[0]]
    except struct.error:
        print("not enough data in buffer to unpack...")
        return mem_data
    # print(f"after final recv in read fifo...")
    return mem_data

#--------------------------------------------------------------------------#
# Pulse Register:
# Reg[15:0] = 
# {5'bxxx,stop_DAQ_pulse,start_DAQ_pulse,start_hist_counter,
# resumePulse,clear_ws_trig_block_pulse,clrError,initPulse,
# errInjPulse,fcStart,fifo_reset,START}

#--------------------------------------------------------------------------#
## software clear fifo
## MSB..10, i.e., trigger pulser_reg[1]
def software_clear_fifo(ss):
    write_pulse_reg(ss, 0x0002)

#--------------------------------------------------------------------------#
## Reset and Resume state machine after exception in Debug Mode
## MSB..10, i.e., trigger pulser_reg[7]
def resume_in_debug_mode(ss):
    write_pulse_reg(ss, 0x0080)

#--------------------------------------------------------------------------#
## software clear error. This should now clear the event counter
## MSB..100000, i.e., trigger pulser_reg[5]
def software_clear_error(ss):
    write_pulse_reg(ss, 0x0020)

#--------------------------------------------------------------------------#
## ws clear trigger block.
## i.e., trigger pulser_reg[6]
def software_clear_ws_trig_block(ss):
    write_pulse_reg(ss, 0x0040)

#--------------------------------------------------------------------------#
## Fast Command Signal Start
## MSB..100, i.e., trigger pulser_reg[2]
def fc_signal_start(ss):
    write_pulse_reg(ss, 0x0004)

#--------------------------------------------------------------------------#
## Fast Command Initialize pulse
## MSB..10000, i.e., trigger pulser_reg[4]
def fc_init_pulse(ss):
    write_pulse_reg(ss, 0x0010)

#--------------------------------------------------------------------------#
## start_hist_counter
## MSB..100000000, i.e., trigger pulser_reg[8]
def start_hist_counter(ss):
    write_pulse_reg(ss, 0x0100)

#--------------------------------------------------------------------------#
## start_DAQ_pulse
## MSB..1000000000, i.e., trigger pulser_reg[9]
def start_DAQ_pulse(ss):
    write_pulse_reg(ss, 0x0200)

#--------------------------------------------------------------------------#
## stop_DAQ_pulse
## MSB..10000000000, i.e., trigger pulser_reg[10]
def stop_DAQ_pulse(ss):
    write_pulse_reg(ss, 0x0400)

#--------------------------------------------------------------------------#
## Register 14
## Enable FPGA Descrambler
## {12'bxxxxxxxxx,add_ethernet_filler,debug_mode,dumping_mode,notGTXPolarity,notGTX,enableAutoSync}
def Enable_FPGA_Descramblber(ss, val=0x000b):
    write_config_reg(ss, 14, val)

#--------------------------------------------------------------------------#
## Register 15
## Reg 15 : {global_trig_delay[4:0],global_trig,trig_or_logic,triple_trig,en_ws_trig,ws_trig_stop_delay[2:0],enableCh[3:0]}
def active_channels(ss, key = 0x0001):
    print(f"writing: {bin(key)} into register 15")
    write_config_reg(ss, 15, key)

#--------------------------------------------------------------------------#
## Register 13
## Reg 13 : {dataRate[1:0],LED_Pages[2:0],status_Pages[1:0]} 
def timestamp(ss, key=0x0000):
    write_config_reg(ss, 13, key)

#--------------------------------------------------------------------------#
## Register 12
## 4-digit 16 bit hex, 0xWXYZ
## WX (8 bit) - Duration
## Y - N/A,N/A,Period,Hold
## Z - Input command
def register_12(ss, key = 0x0000):
    write_config_reg(ss, 12, key)

#--------------------------------------------------------------------------#
## Register 11
## Reg 11 : {4'bxxxx,duration[11:0]} \ Reg 12 : {errorMask[7:0],trigDataSize[1:0],period,1'bx,inputCmd[3:0]} 
def register_11(ss, key = 0x0000):
    write_config_reg(ss, 11, key)

## Register 10
def register_10(ss, prescale_factor, init_address_first = 0x000):
    valid_prescale_factors = {
        2048: 0b00,
        4096: 0b01,
        8192: 0b10,
        16384: 0b11,
    }
    if prescale_factor not in valid_prescale_factors:
        raise RuntimeError("You did not choose a valid prescale factor")
    prescale_bitmask = valid_prescale_factors[prescale_factor]
    key = ((prescale_bitmask & 0b11) << 12) + (init_address_first & 0xfff)
    write_config_reg(ss, 10, key)

## Register 9
def register_9(ss, init_address_last = 0x000):
    key = (init_address_last & 0xfff)
    write_config_reg(ss, 9, key)

#--------------------------------------------------------------------------#
## Register 8
## Reg 8 : {trigSelMask[3:0],enhenceData,enableL1Trig,L1Delay[9:0]} 
def triggerBitDelay(ss, key = 0x0400):
    write_config_reg(ss, 8, key)

#--------------------------------------------------------------------------#
## Register 7
## Reg 7 : {6'bxxxxxx,delayTrigCh[3:0],6'bxxxxxx} //trigbit delay or not 
def counterDuration(ss, key = 0x0000):
    write_config_reg(ss, 7, key)