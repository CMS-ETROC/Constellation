#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import time
import threading
import numpy as np
import os
from queue import Queue
from collections import deque
import queue
import command_interpret
# from translate_data import *
import datetime
#========================================================================================#
'''
@author: Murtaza Safdari
@date: 2023-09-13
This script is composed of all the helper functions needed for I2C comms, FPGA, etc
'''
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
    command_interpret.write_pulse_reg(ss, 0x0002)

#--------------------------------------------------------------------------#
## Reset and Resume state machine after exception in Debug Mode
## MSB..10, i.e., trigger pulser_reg[7]
def resume_in_debug_mode(ss):
    command_interpret.write_pulse_reg(ss, 0x0080)

#--------------------------------------------------------------------------#
## software clear error. This should now clear the event counter
## MSB..100000, i.e., trigger pulser_reg[5]
def software_clear_error(ss):
    command_interpret.write_pulse_reg(ss, 0x0020)

#--------------------------------------------------------------------------#
## ws clear trigger block.
## i.e., trigger pulser_reg[6]
def software_clear_ws_trig_block(ss):
    command_interpret.write_pulse_reg(ss, 0x0040)

#--------------------------------------------------------------------------#
## Fast Command Signal Start
## MSB..100, i.e., trigger pulser_reg[2]
def fc_signal_start(ss):
    command_interpret.write_pulse_reg(ss, 0x0004)

#--------------------------------------------------------------------------#
## Fast Command Initialize pulse
## MSB..10000, i.e., trigger pulser_reg[4]
def fc_init_pulse(ss):
    command_interpret.write_pulse_reg(ss, 0x0010)

#--------------------------------------------------------------------------#
## start_hist_counter
## MSB..100000000, i.e., trigger pulser_reg[8]
def start_hist_counter(ss):
    command_interpret.write_pulse_reg(ss, 0x0100)

#--------------------------------------------------------------------------#
## start_DAQ_pulse
## MSB..1000000000, i.e., trigger pulser_reg[9]
def start_DAQ_pulse(ss):
    command_interpret.write_pulse_reg(ss, 0x0200)

#--------------------------------------------------------------------------#
## stop_DAQ_pulse
## MSB..10000000000, i.e., trigger pulser_reg[10]
def stop_DAQ_pulse(ss):
    command_interpret.write_pulse_reg(ss, 0x0400)

#--------------------------------------------------------------------------#
## Register 14
## Enable FPGA Descrambler
## {12'bxxxxxxxxx,add_ethernet_filler,debug_mode,dumping_mode,notGTXPolarity,notGTX,enableAutoSync}
def Enable_FPGA_Descramblber(ss, val=0x000b):
    command_interpret.write_config_reg(ss, 14, val)

#--------------------------------------------------------------------------#
## Register 15
## Reg 15 : {global_trig_delay[4:0],global_trig,trig_or_logic,triple_trig,en_ws_trig,ws_trig_stop_delay[2:0],enableCh[3:0]}
def active_channels(ss, key = 0x0001):
    print(f"writing: {bin(key)} into register 15")
    command_interpret.write_config_reg(ss, 15, key)

#--------------------------------------------------------------------------#
## Register 13
## Reg 13 : {dataRate[1:0],LED_Pages[2:0],status_Pages[1:0]} 
def timestamp(ss, key=0x0000):
    command_interpret.write_config_reg(ss, 13, key)

#--------------------------------------------------------------------------#
## Register 12
## 4-digit 16 bit hex, 0xWXYZ
## WX (8 bit) - Duration
## Y - N/A,N/A,Period,Hold
## Z - Input command
def register_12(ss, key = 0x0000):
    command_interpret.write_config_reg(ss, 12, key)

#--------------------------------------------------------------------------#
## Register 11
## Reg 11 : {4'bxxxx,duration[11:0]} \ Reg 12 : {errorMask[7:0],trigDataSize[1:0],period,1'bx,inputCmd[3:0]} 
def register_11(ss, key = 0x0000):
    command_interpret.write_config_reg(ss, 11, key)

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
    command_interpret.write_config_reg(ss, 10, key)

## Register 9
def register_9(ss, init_address_last = 0x000):
    key = (init_address_last & 0xfff)
    command_interpret.write_config_reg(ss, 9, key)

#--------------------------------------------------------------------------#
## Register 8
## Reg 8 : {trigSelMask[3:0],enhenceData,enableL1Trig,L1Delay[9:0]} 
def triggerBitDelay(ss, key = 0x0400):
    command_interpret.write_config_reg(ss, 8, key)

#--------------------------------------------------------------------------#
## Register 7
## Reg 7 : {6'bxxxxxx,delayTrigCh[3:0],6'bxxxxxx} //trigbit delay or not 
def counterDuration(ss, key = 0x0000):
    command_interpret.write_config_reg(ss, 7, key)