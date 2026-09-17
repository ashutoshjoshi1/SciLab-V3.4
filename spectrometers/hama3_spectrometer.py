import logging
from ctypes import windll, byref, c_int, c_uint, c_uint32, c_ushort, c_ubyte, c_bool, create_string_buffer
from time import sleep
import numpy as np
import platform
import threading
import os, sys
from copy import deepcopy
from spec_xfus import spec_clock, calc_msl, split_cycles
from datetime import datetime
from collections import OrderedDict



# Logger setup
logger = logging.getLogger(__name__)

if sys.version_info[0] < 3:
    # Python 2.x
    from Queue import Queue
else:
    # Python 3.x
    from queue import Queue


#---Global variables---

#Parameters of the camera (roe)
cameras={"C13015-01":{"tpi_st_min":{"S13496":4500}, #Minimum line cycle for a specific detector [CLK]
                     "tpi_st_max":4294967295, #Maximum line cycle [CLK]
                     "thp_st_min":10, #Minimum high period of ST signal
                     "tlp_st_min":200, #Minimum low period of ST signal (it appears in the manual as thp(ST)max=tpi(ST)-200 )
                     }
        }

#Parameters of the detectors (sensor)
detectors={"S13496":{"tpi_st_min":106, #Minimum start pulse cycle [CLK] ( it appears in the documentation as 106/f(CLK) s )
                     "thp_st_min":6, #Minimum high period of ST signal [CLK]
                     "tlp_st_min":100, #Minimum low period of ST signal [CLK]
                     "it_offset_clk":48}, #Integration time offset in clock pulses (documentation: "The integration time equals the high period of ST plus 48 CLK cycles")
           }

#POssible errors of the DcIc dll
hama3_errors={
    0: "OK",
    1: "Unknown error", #DcIc_ERROR_UNKNOWN
    2: "Initialization not done", #DcIc_ERROR_INITIALIZE
    3: "The parameter is illegal", #DcIc_ERROR_PARAMETER
    4: "The error occurred by the device connection", #DcIc_ERROR_CONNECT
    5: "The error occurred by the device disconnection", #DcIc_ERROR_DISCONNECT
    6: "This control doesn't correspond", #DcIc_ERROR_SEND
    7: "This control doesn't correspond", #DcIc_ERROR_RECEIVE
    8: "Fails in the data receive", #DcIc_ERROR_STOPRECEIVE
    9: "Fails in the close", #DcIc_ERROR_CLOSE
    10: "Memory allocation error", #DcIc_ERROR_ALLOC
    11: "Error in the data measurement", #DcIc_ERROR_CAPTURE
    12: "Timeout error", #DcIc_ERROR_TIMEOUT
    20: "Error DcIc_ERROR_WRITEPROTECT",
    21: "DcIc_ERROR_ILLEGAL_ACCESS",
    22: "DcIc_ERROR_ILLEGAL_ADDR",
    23: "Non valid parameter value", #DcIc_ERROR_ILLEGAL_VALUE",
}


Hama3_Spectrometer_Instances={}
Hama3_devs_info={}


class Hama3_Spectrometer():

    def __init__(self):

        self.spec_type="Hama3"

        #Note: Next to the parameters description there will be an (E = external) or an (I = internal).
        # (E) means that the parameter may be eventually accessed / configured by BlickO.
        # (I) means that the parameter is for internal usage only, and won't be accessed by BlickO.

        #---Parameters---
        self.debug_mode=0 #(E) 0=disabled, >0=enabled. Low numbers are less verbose, high numbers are more verbose.
        #debug_mode=0 will disable the debug mode, only the basic connection/disconnection steps (with self.logger.info()) will be printed in the log files.
        #debug_mode=1 will print the previous plus the debug messages related to start and end of the measurements.
        #debug_mode=2 will print the previous plus the changes of integration time (set_it() function).
        #debug_mode=3 will print the previous plus reset_data, read_data and handle_data actions (every cycle actions).


        self.simulation_mode=False #(E) Set this to True to enable the simulation mode (no dll will be loaded, no spectrometer will be really connected)
        self.dll_logging=False #(E) Set this to True to enable the internal logging of the dll. (Only for debugging purposes)

        #Avantes control DLL path
        self.dll_path="" #(E) Will store the path of the Avantes control dll (string)
        #Spec Parameters:
        self.sn="1102185U1" #(E) Serial number of the spectrometer to be used (string)
        self.sensor_model="S13496" #(I) Sensor model of the spectrometer. (string)
        self.camera_model="C13015-01" #(I) Camera model of the spectrometer. (string)
        self.clock_frequency_mhz=10 #(I) Internal clock frequency of the spectrometer in MHz (integer)
        self.alias="1" #(E) Spectrometer index alias (string), i.e: "1" for spec1, or "2" for spec2. Just to
        # identify the spec index in the log files, for when there is more than one spectrometer connected.
        self.npix_active=4096 #(E) Number of active pixels of the detector (integer)
        #this device does not support blind pixels.
        self.npix_vert=1 #(I) number of vertical pixels (or lines)
        self.nbits=16 #(E) Number of bits of the spectrometer detector A/D converter (integer)
        self.max_it_ms=4000.0 #(E) Maximum integration time allowed by the spectrometer, in milliseconds (float) (=Max exposure time)
        self.min_it_ms=2.4 #(E) Minimum integration time allowed by the spectrometer, in milliseconds (float) (=Min exposure time)
        self.discriminator_factor=1.0 #(E) Discriminator factor to be applied to the counts. Received raw counts will be multiplied by this factor. (float)
        self.gain_detector=-1 #(E) Gain of the detector; -1 = do not set, 0=set to Low gain, 1=set to High gain at connection time. (Only valid for specific detectors)
        self.gain_roe=-1 #(E) Gain of the roe AD converter; -1 = do not set, 0=set to Low gain, 1=set to High gain at connection time.
        self.offset_roe=-1 #(E) Offset of the roe AD converter; -1 = do not set, any other value will be set at connection time.
        self.eff_saturation_limit=2**self.nbits-1 #(E) Effective saturation limit of the detector [max counts] (integer). Measurements containing counts above this limit will be considered as saturated.
        self.cycle_timeout_ms=4000 #(I) Timeout to have 1 cycle of data ready (integer, milliseconds). If the data does not arrive after the integration time + this timeout, a timeout error will be raised.

        #Working mode:
        self.abort_on_saturation=True #(E) boolean - If True, the measurement will be aborted as soon as saturated signal
        # is detected in the latest cycle read data. An order to abort the rest of the measurement will be sent to the spectrometer
        # and no more data will be handled from that moment. The output data would be still usable, but it would only
        # contain the non-saturated cycles (if any).
        self.max_ncy_per_meas=100 #(I) Maximum number of cycles to measure per measurement order (integer).
        # if max_ncy_per_meas>1 "measure by packs" will be enabled.

        #Performance tests:
        self.performance_test_it_ms_list=np.arange(2.4,10.1,0.1) #(I) List or Array with the different integration times to be tested during the performance test. [ms]
        self.performance_test_ncy_list=[1,10,30,50,70,90,110,130,150] #(I) List or Array with the different number of cycles to be tested during the performance test. [list of int]

        #Simulation mode parameters:
        self.simulated_rc_min=int(0.2*(2**self.nbits-1)) #Min and max raw counts to be measured in simulation mode (integer>0)
        self.simulated_rc_max=int(0.5*(2**self.nbits-1))
        self.simudur=0.1 #Duration of the simulated measurements with respect the original duration. (IT will be reduced by this factor, for faster simulations) (float)


        #--------Do not modify anything below this line, the following variables are for internal usage only----
        self.dll_handler=None #(E) Will store the handler to the Avantes control dll (ctypes.CDLL object)
        self.spec_id=None #(E) Will store the spectrometer id, used by some dll functions in order to point to one specific spectrometer device (byte string)
        self.parlist=None #(E) Will be used to store the low level parameter list (internal configuration parameters of the spectrometer).
        self.it_ms=None #(E) Will store the currently set integration time in milliseconds. Use set_it() to change it.
        self.logger=None #(E) Will store the logger object for one specific spectrometer (logging.Logger object, see initialize_spec_logger())
        self.product_id=None #(I) Will store the spectrometer product id, used by the dll to initialize itself for a specific spectrometer model
        self.devtype=None #(I) Will store the spectrometer device type (ROE type, ie AS5216 or AS7010) (string). Used for internal recovery protocols.

        #Internal variables for measure control
        self.measuring=False #(E) Will be used to indicate that the spectrometer is measuring (boolean)
        self.recovering=False #(E) Will be used to indicate that the spectrometer is in recovery mode (boolean) -> it will disable the external_meas_done_event
        self.docatch=False #(E) Will be used to "hear" for data arrival events. If False, the data arrival events will be ignored.
        self.ncy_requested=0 #(E)Will store the total number of cycles requested (integer)
        self.ncy_per_meas=[1] #(I) Depending on max_ncy_per_meas, the cycles will be measured in packs. This list of integers will store how many cycles are going to be requested in each measurement call. I.e: if max_ncy_per_meas=10 and ncy_requested=21, then ncy_per_meas=[10,10,1]
        self.ncy_read=0 #(E) Will store the current number of cycles already read (integer)
        self.ncy_handled=0 #(E) Current number of cycles handled
        self.ncy_saturated=0 #(E) Will store how many saturated cycles there are in the handled data (integer)
        self.internal_meas_done_event=threading.Event() #(I) This internal event will be "unset" whenever a measurement is started, and "set" when the measurement is complete (all ncy read and handled). Its usage is internal: just for this module.

        #Internal variables for data output:
        self.rcm=np.array([]) #(E) Will store the mean raw counts of the measurements (numpy array)
        self.rcs=np.array([]) #(E) Will store the (sample) standard deviation of the raw counts of the measurements (numpy array)
        self.rcl=np.array([]) #(E) Will store the rms of the standard deviation fitted to a straight line
        self.sy=np.zeros(self.npix_active,dtype=np.float64) #Sum of the counts
        self.syy=np.zeros(self.npix_active,dtype=np.float64) #Sum of the squared counts
        self.sxy=np.zeros(self.npix_active,dtype=np.float64) #Sum of the meas index by the counts
        self.arrival_times=[] #List of arrival times of the measurements
        self.meas_start_time=0 #Unix time in seconds when the measurement started
        self.meas_end_time=0 #Unix time in seconds when the measurement ended (data arrival time of the last measured cycle)
        self.data_handling_end_time=0 #Unix time in seconds when the data handling ended (all cycles received + handled + final data handling)

        #Post-processing actions
        self.external_meas_done_event=None #(E) External event to be set when a measurement is complete (apart from the internal_meas_done_event). (None or threading.Event object, Optional).
        #This external event will be used to notify to other parent modules that a measurement is complete.
        #Note: This external event must be unset by the parent module, this module will not unset it.

        #Internal variables to get data and handle data (queues and threads):
        self.read_data_queue=Queue() #(I) Will store the get data queue. When a measurement is ready, a flag will be put here by measure_callback().
        self.handle_data_queue=Queue() #(I) Will store the data arrival queue. When a measurement is done, the data will be put here for subsequent data handling.
        self.data_arrival_watchdog_thread=None #(I) Will store the data arrival watchdog thread
        self.data_handling_watchdog_thread=None #(I) Will store the data handling watchdog thread


        #Error Handling
        self.errors=hama3_errors #(E) Dictionary with the possible errors of the dll. The keys are the error codes, and the values are the error descriptions.
        self.error="OK" #(E) Latest generated error description (string). See self.errors for a list of possible error descriptions.
        # It can also be "OK" if no error, or "UNKNOWN_ERROR" if the dll returned an error code that is not in the list of known errors.

        self.last_errcode=0 #(I) Latest generated dll error code (integer). See self.errors for a list of possible error codes.

    #---Main Control Functions (used by BlickO)---

    def initialize_spec_logger(self):
        """Initialize the dedicated logger for spectrometer."""
        self.logger = logging.getLogger("spec"+self.alias)

    def connect(self):
        """
        Connects to the spectrometer and initializes it.
        """
        ndev=0

        #Reset data:
        self.reset_spec_data()

        if self.simulation_mode:
            self.logger.info("--- Connecting spectrometer "+self.alias+"... (Simulation Mode ON) ---")
            self.spec_id=1
            #Set initial integration time:
            res=self.set_it(self.min_it_ms)
        else:
            self.logger.info("--- Connecting spectrometer "+self.alias+"... ---")

            #Load the spectrometer control dll:
            res=self.load_spec_dll() #This will be only needed at initial connection.
            
            #Initialize dll
            if res=="OK":
                res=self.initialize_dll() #AVS_Init()

            #Enable/disable dll debug mode:
            # if res=="OK":
            #     res=self.enable_dll_logging(enable=self.dll_logging)

            #Check number of USB connected devices is >0
            if res=="OK":
                res,ndev=self.get_number_of_devices() #AVS_UpdateUSBDevices()

            #Connect to every device and get the serial number:
            # (I could not find a way to get the serial number without connecting first to each device)
            if res=="OK":
                self.logger.info("Getting "+self.spec_type+" spectrometers info...")
                for i in range(ndev): #device object index
                    if i not in Hama3_devs_info:
                        #Connect to the specific spectrometer
                        spec_id=self.dll_handler.DcIc_Connect(c_uint(i))
                        if spec_id<=0:
                            res="Cannot connect to spectrometer of type "+self.spec_type+". Connection error code: "+str(spec_id)
                            self.logger.warning(res)
                            continue

                        #Get device information of the connected device
                        res,dev_info=self.get_dev_info(spec_id)
                        if res!="OK":
                            res="Could not get device information of the spectrometer "+self.alias+", error: "+res
                            self.logger.warning(res)
                            continue

                        self.logger.info("Found spec device : "+", ".join([k+"="+str(v) for k,v in dev_info.items()]))

                        #Store the device information into a Global variable so that is not needed to connect again
                        # to the same device just to get info from it.
                        Hama3_devs_info[i]=dev_info

                        #Disconnect from the device
                        _=self.dll_handler.DcIc_Disconnect(spec_id)


            #Now with the Hama3_devs_info dictionary filled, find the spectrometer with the correct serial number:
            dev_index=None
            if res=="OK":
                for dev_index in Hama3_devs_info.keys():
                    dev_sn=str(Hama3_devs_info[dev_index].get("sn","")).strip("\x00").strip()
                    if dev_sn==self.sn:
                        break #-> dev_index remains with the correct index
                else: #No break happened
                    res="Could not find the spectrometer of type "+self.spec_type+\
                        " with serial number "+self.sn+" connected through USB."
                    if len(Hama3_devs_info)>0:
                        res+="\nConnected devices found: "+str([str(Hama3_devs_info[key].get("sn","")).strip("\x00").strip() for key in Hama3_devs_info])


            #Connect to the spectrometer with the correct serial number:
            if res=="OK":
                self.spec_id=self.dll_handler.DcIc_Connect(c_uint(dev_index))
                if self.spec_id<=0:
                    res="Cannot connect to spectrometer of type "+self.spec_type+". Connection error code: "+str(self.spec_id)
                    self.logger.warning(res)

            #Add device to the Hama3_Spectrometer_Instances dictionary
            if res=="OK":
                Hama3_Spectrometer_Instances[self.spec_id]=self

            #Send an abort order to the spectrometer (just in case)
            if res=="OK":
                res=self.abort(ignore_errors=True)

            #Get & Check number of active pixels
            if res=="OK":
                npix_c=c_ushort()
                resdll=self.dll_handler.DcIc_GetHorizontalPixel(self.spec_id,byref(npix_c))
                res=self.get_error(resdll)
                if res!="OK":
                    res="Cannot get number of active pixels of the spectrometer "+self.alias+", error: "+res
                else:
                    #Check if npix is correct:
                    npix_active=npix_c.value
                    if self.npix_active != npix_active:
                        res="Number of active pixels of spec "+self.alias+" is "+str(npix_active)+", expected "+str(self.npix_active)+". Check IOF parameters."

            #Get & Check number of blind pixels
            #-> This device does not support blind pixels.

            #Get & Check number of vertical pixels
            if res=="OK":
                npix_c=c_ushort()
                resdll=self.dll_handler.DcIc_GetVerticalPixel(self.spec_id,byref(npix_c))
                res=self.get_error(resdll)
                if res!="OK":
                    res="Cannot get number of vertical pixels of the spectrometer "+self.alias+", error: "+res
                else:
                    #Check if npix is correct:
                    npix_vert=npix_c.value
                    if self.npix_vert != npix_vert:
                        res="Number of vertical pixels of spec "+self.alias+" is "+str(npix_vert)+", expected "+str(self.npix_vert)+". Check IOF parameters."

            #Set detector gain (only if gain_detector is not -1)
            if res=="OK" and self.gain_detector!=-1:
                gain_detector="High" if self.gain_detector else "Low"
                res=self.set_gain_detector(gain_detector)

            #Set roe gain (only if gain_roe is not -1)
            if res=="OK" and self.gain_roe!=-1:
                gain_roe="High" if self.gain_roe else "Low"
                res=self.set_gain_roe(gain_roe)



            #Set cooler settings


            #Set initial integration time:
            if res=="OK":
                #set integration time to the double of the minimum and then the minimum
                #to check whether integration time change works
                for it in [self.min_it_ms * 2,self.min_it_ms]:
                    res=self.set_it(it)
                    if res!="OK":
                        break
                sleep(0.2)

            #Set data arrival timeout:
            if res=="OK":
                #We measure cycle by cycle, so this is the timeout for each cycle.
                #If after waiting for the integration time, the measurement does not arrive in another
                #cycle_timeout_ms, then a timeout error will be raised.
                resdll=self.dll_handler.DcIc_SetDataTimeout(self.spec_id, c_int(int(self.cycle_timeout_ms)))
                res=self.get_error(resdll)
                if res!="OK":
                    res="connect, could not set cycle timeout. error:"+str(res)


        #Create side threads with the "data arrival watchdog" and the "data handling watchdog":
        if res=="OK":
            if self.data_arrival_watchdog_thread is None:
                self.logger.info("Starting data arrival watchdog thread...")
                self.data_arrival_watchdog_thread=threading.Thread(target=self.data_arrival_watchdog)
                self.data_arrival_watchdog_thread.start()
            if self.data_handling_watchdog_thread is None:
                self.logger.info("Starting data handling watchdog thread...")
                self.data_handling_watchdog_thread=threading.Thread(target=self.data_handling_watchdog)
                self.data_handling_watchdog_thread.start()
            self.logger.info("Spectrometer connected.")

        #Update last error
        self.error = res

        return res

    def set_it(self, it_ms):
        """
        Set the integration cycle time (exposure time).
        This Hama3 camera (DcIc dll) needs to have the integration time set in number of clock cycles (CLK).

        If the requested it_ms is beyond the limits, the function will return an error message
        and the integration time won't be set.
        """
        res,high_period,low_period,line_cycle=self.compute_st_pulses(it_ms,
                                                                     clock_frequency_mhz=self.clock_frequency_mhz,
                                                                     camera=self.camera_model,
                                                                     sensor=self.sensor_model)

        if res=="OK":
            thp_st=c_uint32(high_period) #DWORD
            tpi_st=c_uint32(line_cycle) #DWORD

            if self.simulation_mode:
                if self.debug_mode>=2:
                    self.logger.debug("Setting integration time to "+str(it_ms)+" ms, (line cycle="+str(tpi_st.value)+
                                      " CLK, high st signal="+str(thp_st.value)+" CLK) (Simulation mode)")
                res="OK"
            else:
                if self.debug_mode>=2:
                    self.logger.debug("Setting integration time to "+str(it_ms)+" ms, (line cycle="+str(tpi_st.value)+
                                      " CLK, high st signal="+str(thp_st.value)+" CLK)")

                #Set (preliminar) integration time to the minimum: (just to ensure that the Line cycle to be set later is longer)
                preliminar_thp_st=c_uint32(cameras[self.camera_model]["thp_st_min"]) #DWORD
                resdll=self.dll_handler.DcIc_SetStartPulseTime(self.spec_id, preliminar_thp_st)
                res=self.get_error(resdll)
                if res!="OK":
                    res="set_it, Could not set preliminary Start Pulse Time to "+str(preliminar_thp_st.value)+" CLK, error: "+res

                #Set Line Cycle [CLK]
                if res=="OK":
                    resdll=self.dll_handler.DcIc_SetLineTime(self.spec_id, tpi_st)
                    res=self.get_error(resdll)
                    if res!="OK":
                        res="set_it, Could not set Line Time to "+str(tpi_st.value)+" CLK, error: "+res

                #Set (final) Integration Time [CLK]
                if res=="OK":
                    resdll=self.dll_handler.DcIc_SetStartPulseTime(self.spec_id, thp_st)
                    res=self.get_error(resdll)
                    if res!="OK":
                        res="set_it, Could not set Start Pulse Time to "+str(thp_st.value)+" CLK, error: "+res

        else: #IT out of limits, get limits for warning message:
            min_it_ms,min_it_clk=self.compute_camera_it_min(clock_frequency_mhz=self.clock_frequency_mhz,
                                                            camera=self.camera_model)
            res="Cannot set IT because is out of limits."
            res+=" Minimum IT allowed by the camera roe is "+str(min_it_ms)+" ms ("+str(min_it_clk)+" CLK)"

        #Update currently set IT
        if res=="OK":
            self.it_ms = it_ms

        #Update last error
        self.error = res
        return res

    def set_it_old(self, it_ms):
        """
        Set the integration cycle time (exposure time).
        This is an old version.
        """
        sensor_min_tpi_st = 4500 #Minimum Line Cycle of the camera, for a given sensor [CLK], -> At C13015-01 Driver documentation, for sensor S13496, it is 4500 CLK.
        sensor_clock_freq_ms = 10000 #10MHz = 10.000.000 cycles/s = 10.000 cycles per ms.
        #sensor_it_clk_offset = 48 #S13496 Detector documentation: "The integration time equals the high period of ST plus 48 CLK cycles"
        sensor_it_clk_offset = 0  # Made this zero to make it compatible with all detectors
        # (and to match the DcIcUSB software, which is sensor agnostic)
        #This means that when 0.001ms IT is selected in BlickO (10 CLK), the real IT set at the detector will be 0.0058ms (10+48 CLK).
        thp_st = int(it_ms * sensor_clock_freq_ms) - sensor_it_clk_offset #high period of ST signal, [CLK]
        tpi_st = thp_st + 200 #Line Cycle [CLK]. From C13015-05 Driver documentation: Max thp_st = tpi_st - 200
        if tpi_st < sensor_min_tpi_st:
            tpi_st = sensor_min_tpi_st
        elif tpi_st > 4294967295:
            tpi_st = 4294967295

        thp_st=c_uint32(thp_st) #DWORD
        tpi_st=c_uint32(tpi_st) #DWORD


        if self.simulation_mode:
            if self.debug_mode>=2:
                self.logger.debug("Setting integration time to "+str(it_ms)+" ms, (line cycle="+str(tpi_st.value)+
                                  " CLK, high st signal="+str(thp_st.value)+" CLK) (Simulation mode)")
            res="OK"
        else:
            if self.debug_mode>=2:
                self.logger.debug("Setting integration time to "+str(it_ms)+" ms, (line cycle="+str(tpi_st.value)+
                                  " CLK, high st signal="+str(thp_st.value)+" CLK)")

            #Set Line Cycle [CLK]
            resdll=self.dll_handler.DcIc_SetLineTime(self.spec_id, tpi_st)
            res=self.get_error(resdll)
            if res!="OK":
                res="set_it, Could not set Line Time to "+str(tpi_st)+" CLK, error: "+res
            else:
                #Set Integration Time [CLK]
                resdll=self.dll_handler.DcIc_SetStartPulseTime(self.spec_id, thp_st)
                res=self.get_error(resdll)
                if res!="OK":
                    res="set_it, Could not set Start Pulse Time to "+str(thp_st)+" CLK, error: "+res

        #Update currently set IT
        self.it_ms = it_ms

        #Update last error
        self.error = res
        return res

    def measure(self,ncy=1):
        """
        res=measure(ncy=1)

        params:
            <ncy>: Number of cycles to measure (integer)

        This function will request to the spectrometer to start a ncy cycles measurement.
        It is a non-blocking call.

        This function will put a signal into the read_data_queue queue, to indicate to the data_arrival_watchdog thread
        that a measurement of a certain number of cycles is requested.

        The data_arrival_watchdog thread will notice this signal and will start the measurement through the
        measure_blocking(ncy) function, which is a blocking call running in a side thread.

        The cycles are measured one by one or in packs (depending on the self.max_ncy_per_meas parameter), and
        every time a cycle or pack of cycles is measured, the data is read and put into the handle_data_queue
        to indicate to the data_handling_watchdog thread that new data is ready to be handled.

        The data handling watchdog thread will notice this new data flag and will proceed to handle the data
        in a side thread.

        Once all cycles are measured, read, and handled, the data_handling_watchdog thread will do the
        "final data handling" (calculate the mean, standard deviation, and rms fitted to line) and then it sets the
        "internal_meas_done_event" event, to indicate to the measure_blocking function that all cycles has been handled,
        so that the measurement can be considered as finished.

        Finally, if the "external_meas_done_event" is not None, the measure_blocking function will be set this event
        in order to indicate to any parent module (out of this library) that the measurement is complete.
        (This is the event used to notify a measurement finished to the spec_controller module of BlickO)

        This can be much simpler, but in this way we ensure the spec is already measuring the next cycle while
        we are handling the data of the previous cycle.
        """
        self.internal_meas_done_event.clear()
        self.measuring=True
        self.docatch=True
        self.error="OK" #Reset last error

        #Put ncy into the data_read_queue:
        self.read_data_queue.put(ncy)

        return self.error

    def abort(self, ignore_errors=False, log=True, disable_docatch=True):
        """
        res=self.abort(ignore_errors=False, log=True, disable_docatch=True)

        Send an order to the spectrometer in order to stop any ongoing measurement.

        params:
         <ignore_errors>: if True, ignore any error that may happen while stopping the measurement (boolean)
         <log>: If True, logging entries will be added to the console/logfile (boolean)
         <disable_docatch>: If True, self.docatch will be set to false.  (boolean)

        return:
            <res>: string with the result of the operation. It can be "OK" or an error description.
        """
        if disable_docatch:
            self.docatch=False # -> Stop adding new data arrivals into the read data queue.
        res="OK"
        if self.simulation_mode:
            if log:
                self.logger.info("abort, stopping any ongoing measurement (simulation mode)...")
        else:
            if log:
                self.logger.info("abort, stopping any ongoing measurement...")
            try:
                resdll=self.dll_handler.DcIc_Abort(self.spec_id)
                res=self.get_error(resdll)
                if res!="OK": #Wrong answer
                    res="Spec "+self.alias+", could not stop any ongoing measurement. Error: "+res
                    if ignore_errors: #Just warn, but return OK
                        self.logger.warning("abort, "+res)
                        res="OK"
                    else:
                        self.logger.error("abort, "+res)
            except Exception as e:
                    res="Exception happened while stopping any ongoing measurement: "+str(e)
                    if ignore_errors: #Just warn, but return OK:
                        self.logger.warning(res)
                        res="OK"
                    else:
                        self.logger.exception(e)

        self.error=res

        return res

    def read_aux_sensor(self,sname="detector"):
        """
        Read an auxiliary sensor of the spectrometer. (Temperature)
        """
        res="OK"
        if self.simulation_mode:
            value=11.1
            return res, value

        value=-99.0 #Default preliminary value
        func=None
        if sname == "detector": #Temperature at detector
            func=self.dll_handler.DcIc_GetTemperature1
            # Note that this function returns DcIc_ERROR_ILLEGAL_ADDR error,
            # when it is used with the C13015-01 camera roe
            vcut=99.0
        elif sname in ["board_analog","board_digital"]: #Temperature at board
            func=self.dll_handler.DcIc_GetTemperature2
            #Note that this function returns DcIc_ERROR_ILLEGAL_ADDR error,
            # when it is used with the C13015-01 camera roe
            vcut=99.0
        else:
            res="Unknown sensor name: '"+sname+"' for spec "+self.alias
            self.logger.error(res)
        if res=="OK":
            vc=c_ushort()
            resdll=func(self.spec_id,byref(vc))
            res=self.get_error(resdll)
            if res=="OK":
                v=float(vc.value)
                if v<vcut: #There is a sensor
                    value=v
                    if self.debug_mode>=2:
                        self.logger.debug("read_aux_sensor, '"+sname+"' sensor value: "+str(value))
                else:
                    self.logger.warning("read_aux_sensor, '"+sname+"' sensor value is out of range: "+str(v))
            else:
                self.logger.warning("read_aux_sensor, could not read '"+sname+"' aux sensor, err= "+res)

        return res,value

    def disconnect(self, dofree=False, ignore_errors=False):
        """
        Deactivate and disconnect spectrometer from dll.
        params:
            <dofree>: (boolean) Set this to True to finalize the dll communication as well (clear dll_handler).
            Note that this parameter is only having effect when there is only one spectrometer connected
            (ie when all the other spectrometers have been already disconnected). Otherwise, the dll communication
            won't be closed.

            <ignore_errors>: (boolean) Set this to True to ignore any error that happens during the disconnection process.

        """
        res="OK"

        #Remove the instance from the Avantes_Spectrometer_Instances dictionary: So that no more data is read from this spectrometer.
        if self.spec_id in Hama3_Spectrometer_Instances:
            del Hama3_Spectrometer_Instances[self.spec_id]

        if self.simulation_mode:
            self.logger.info("Disconnecting spectrometer "+self.alias+"... (Simulation mode)")
        else:
            self.logger.info("Disconnecting spectrometer "+self.alias+", dofree="+str(dofree))
            if self.dll_handler is not None:
                #Deactivate spec: Closes communication with selected spectrometer (clear self.spec_id)
                resdll=self.dll_handler.DcIc_Disconnect(self.spec_id)
                if not ignore_errors:
                    r=self.get_error(resdll)
                    if r!="OK":
                        self.logger.error("disconnect, Could not disconnect device, error: "+r)

                if dofree:
                    self.logger.info("Terminating dll session...")
                    resdll=self.dll_handler.DcIc_Terminate()
                    if not ignore_errors:
                        r=self.get_error(resdll)
                        if r!="OK":
                            self.logger.error("disconnect, Could not terminate dll, error: "+r)


        if self.data_arrival_watchdog_thread is not None:
            self.logger.info("Closing data arrival watchdog thread of spectrometer "+self.alias+".")
            self.read_data_queue.put(None) #Send a "stop" signal to the data handling watchdog thread.
            self.data_arrival_watchdog_thread.join() #Wait for the data arrival watchdog thread to finish.
            self.data_arrival_watchdog_thread=None
        if self.data_handling_watchdog_thread is not None:
            self.logger.info("Closing data handling watchdog thread of spectrometer "+self.alias+".")
            self.handle_data_queue.put((None,None,(None,None,None))) #Send a "stop" signal to the data arrival watchdog thread.
            self.data_handling_watchdog_thread.join() #Wait for the data handling watchdog thread to finish.
            self.data_handling_watchdog_thread=None

        self.logger.info("Spectrometer "+self.alias+" disconnected.")

        self.error=res
        return res

    def recovery(self,ntry=3,dofree=False):
        """
        res=recovery(ntry=3)

        "Soft recovery" or "smooth recovery" of the spectrometer, by sending software commands.
        This function will be used to recover the control of a spectrometer that is not responding by either,
        too long integration time measurement, measurement data never arriving, or
        unexpected spectrometer power loss.

        It just recovers the control, but it does not to measure again the last interrupted
        measurement automatically.

        params:
            <ntry>: number of soft recovery cycles allowed, to try to recover the spectrometer. (integer)
            <dofree>: (boolean) is True, it will finalize the dll communication with AVS_Done() and
             (clear the dll_handler) as a first step. Note that this affects to ALL connected avantes spectrometers,
             so ensure all other spectrometers are idle (not measuring).

        """
        res="NOK"
        self.docatch=False # -> Stop adding new data arrivals into the read data queue.

        for i in range(ntry):

            self.logger.warning("Recovering spectrometer "+self.alias+"... (try "+str(i+1)+"/"+str(ntry)+"), dofree="+str(dofree))

            if i==0 and dofree:
                res="NOK"
            else:
                #Try to stop any ongoing measurement:
                res=self.abort(ignore_errors=False)
                sleep(0.5)

            if res=="OK":
                #The spectrometer was able to abort the ongoing measurement: Then communication is still ok.
                self.logger.info("Spectrometer "+self.alias+" could abort any ongoing measurement.")

                #Set the last integration time set:
                res=self.set_it(it_ms=self.it_ms)
                sleep(0.5) #wait a bit for the integration time to be set.

                if res=="OK":
                    #the spectrometer was able to set the last integration time (and config) successfully:
                    self.logger.info("Spectrometer "+self.alias+" could set the last integration time. -> soft recovery finished successfully.")
                    break #Quit ntry for loop and exit

            if res!="OK":
                #If the code reach this point, the most possible reason is that the spectrometer lost its power while it was measuring
                #and the old dll handler is not valid anymore. It is needed to re-initialize the dll and re-connect the spec.

                #Try to disconnect the spectrometer, and finish the data arrival and data handling watchdog threads:
                dofree_now=True if i==0 and dofree else False
                _=self.disconnect(dofree=dofree_now) #Note: we need to use dofree otherwise it won't work. But this has a side effect:
                #it will affect to all connected spectrometers.
                sleep(2) #wait a bit

                #Try a software hard-reset: This only works in the newest spectrometers (AS7010, AS7007 ROE, but not in AS5216)
                if self.devtype in ["C13015-01"]:
                    res=self.reset_device()

                #Try to connect the spectrometer again:
                res=self.connect()

                if res=="OK":
                    self.logger.info("Spectrometer "+self.alias+" recovered after stopping the last measurement, disconnecting, and re-connecting.")
                    break

                else:
                    self.logger.warning("Recovery of spectrometer "+self.alias+" failed.")
                    sleep(5)

        return res


    #---Main Control Functions (used by this library)---

    def measure_blocking(self, ncy=10):
        """
        Send an order to the spectrometer, in order to get ncy measurements.
        It is a blocking call until the spectrometer finishes the entire measurement

        Note that depending on max_ncy_per_meas, the cycles will be measured one by one, or in packs.
        """
        res = "OK"

        # Abort any ongoing measurement
        _ = self.abort(ignore_errors=True,log=False)
        self.internal_meas_done_event.clear()
        self.measuring = True
        self.docatch = True
        self.ncy_requested = ncy
        self.reset_spec_data()  # Reset accumulated data and measurement counters

        #Get a list with the number of cycles to request in every measurement call
        # i.e: if ncy=21 and max_ncy_per_meas=10 -> ncy_per_meas=[10,10,1], packs_info="2x10cy+1x1cy"
        self.ncy_per_meas,packs_info=split_cycles(self.max_ncy_per_meas, ncy)
        ncalls=len(self.ncy_per_meas) #Number of measurement calls to be done (=number of cycle packs to measure)

        if self.debug_mode > 0:
            self.logger.info("Starting measurement, ncy="+str(ncy)+", IT="+str(self.it_ms)+" ms, npacks="+packs_info)

        ntry_per_call = 3  # Maximum retries per measurement call
        self.meas_start_time = spec_clock.now()  # Record measurement start time

        for call_index in range(ncalls):

            if not self.docatch:
                break # Exit measurement calls loop, abort further measurement calls

            ncy_pack=self.ncy_per_meas[call_index] #Number of cycles to measure in this call iteration

            for attempt in range(1, ntry_per_call + 1):

                #Measure pack of cycles and get data
                res,raw_data,arrival_time=self.measure_pack(ncy_pack)
                if res=="OK":
                    if self.debug_mode >= 2:
                        self.logger.debug("Data arrived for measurement call "+str(call_index+1)+"/"+str(ncalls)+", ncy_pack="+str(ncy_pack))

                    #Enqueue data into the data_handling_queue
                    self.handle_data_queue.put((call_index, #measurement index
                                                arrival_time, #arrival_time,
                                                (deepcopy(raw_data), [], []))) # a deepcopy of the raw data (active pixels, blind left, blind right).
                    break # -> Quit the attempts loop; move to next call (pack of cycles) or finish with res="OK".
                else: #Error happened while measuring the pack, or the measurement was aborted while waiting for data
                    if not self.docatch:
                        #In case the measurement has been aborted (i.e. when saturation has been detected in a previously
                        # measured & handled cycle, and abort due to saturation is enabled) -> ignore the measure_pack result
                        # since data is not going to be used anyway.
                        res="OK"
                        break # -> Quit the attempts loop with res=="OK"; the outer call_index loop will break at next iteration
                    else: #Other error
                        res="Error happen at measurement call "+str(call_index+1)+"/"+str(ncalls)+", ncy_pack="+str(ncy_pack)+": "+res
                        logger.warning(res+" Re-trying, attempt "+str(attempt)+"/"+str(ntry_per_call))
                        continue # -> Next attempt, or finish the attempts loop with res!="OK"

            else: #No break happened in the attempt loop
                # If no successful call after ntry retries
                res = "measure_blocking, measurement failed, "+res
                self.logger.error(res)
                break  # Exit measurement calls loop, abort further measurement calls

        # Wait until all data is handled
        if res=="OK":
            if self.debug_mode > 2:
                self.logger.info("Waiting for data handling to finish...")
            _=self.wait_for_measurement()
        else:
            #Empty the handle_data_queue (discard data)
            while not self.handle_data_queue.empty():
                _=self.handle_data_queue.get()

        # Final cleanup
        self.measuring = False
        self.error=res

        # Inform (any) parent module that the measurement is complete
        if self.external_meas_done_event is not None and not self.recovering:
            self.external_meas_done_event.set()

        return res

    def wait_for_measurement(self):
        """
        res=wait_for_measurement()
        Wait until all data read is already handled.
        returns:
            <res>: If any problem happened while measuring, the last produced error will be stored in self.error
        """
        self.internal_meas_done_event.wait()
        return self.error

    def initialize_dll(self):
        """
        res=self.initialize_dll()

        Initializes the spectrometer dll
        This function tries to open a communication port for all connected devices.

        returns:
        <res>: string with the result of the operation. It can be "OK" or an error description.
        """
        res="OK"

        self.logger.info("Initializing spec "+self.alias+" dll...")

        if self.simulation_mode:
            pass
        else:
            try:
                #Call AVS_Init()
                resdll=self.dll_handler.DcIc_Initialize()
                res=self.get_error(resdll)
                if res!="OK":
                    res="Could not initialize dll, error: "+res
            except Exception as e:
                res="Exception happened while initializing the dll: "+str(e)
                self.logger.exception(e)

        self.error=res #Update last error.

        return res

    def get_number_of_devices(self):
        """
        res,ndev=self.get_number_of_devices()

        Returns the number of spectrometers connected to the computer.

        :return:
        <res>: string with the result of the operation. It can be "OK" or an error description.
        <ndev>: number of spectrometers connected to the computer (integer)
        """
        res="OK"
        ndev=0

        if self.simulation_mode:
            self.logger.debug("get_number_of_devices, simulating a connected "+self.spec_type+" spectrometer device.")
            ndev=1
        else:
            try:
                device_count=c_int()
                resdll=self.dll_handler.DcIc_CreateDeviceInfo(byref(device_count)) #Returns True or False
                res=self.get_error(resdll)
                if res!="OK":
                    res="Could not get number of devices, error: "+res
                    self.logger.error("get_number_of_devices, "+res)
                else:
                    ndev=device_count.value
                    if ndev==0:
                        res="Cannot detect any "+self.spec_type+" spectrometer connected through USB."
                        self.logger.error("get_number_of_devices, "+res)
                    else:
                        self.logger.info("get_number_of_devices, found "+str(ndev)+" "+self.spec_type+" spectrometers connected through USB.")
            except Exception as e:
                res="Exception happened while getting the number of "+self.spec_type+" spectrometers: "+str(e)
                self.logger.exception(e)

        self.error=res #Update last error.
        return res,ndev

    def get_dev_info(self,dev_id):
        """
        res,dev_info=self.get_dev_info()

        Returns the device information of a given camera device identifier object.
        Note that this device identifier object is given by the connect function,
        so it is needed to connect first to get this id.

        return:
            <res>: string with the result of the operation. It can be "OK" or an error description.
            <dev_info>: device information of all connected spectrometers
        """
        self.logger.info("Getting device information of spectrometer device id "+str(dev_id))

        dev_info=OrderedDict()
        dev_info["dev_id"]=dev_id

        #Get number of device (id), which will be used for identify the device from the dll P.O.V.
        buff=create_string_buffer(256)
        resdll=self.dll_handler.DcIc_GetCameraNumber(c_int(dev_id),byref(buff))
        res=self.get_error(resdll)
        if res!="OK":
            res="Cannot get camera number of device "+str(dev_id)+", error: "+res
            return res, dev_info
        raw_val=buff.value
        dev_info["dev_type"]=(raw_val.decode("ascii","replace") if isinstance(raw_val, bytes) else str(raw_val)).strip("\x00").strip()

        #Get serial number
        buff=create_string_buffer(17)
        resdll=self.dll_handler.DcIc_GetSerialNumber(c_int(dev_id),byref(buff))
        res=self.get_error(resdll)
        if res!="OK":
            return "Cannot get serial number of device "+str(dev_id)+", error: "+res, dev_info
        raw_val=buff.value
        dev_info["sn"]=(raw_val.decode("ascii","replace") if isinstance(raw_val, bytes) else str(raw_val)).strip("\x00").strip()

        #Get hardware revision
        buff=c_ubyte()
        resdll=self.dll_handler.DcIc_GetHWRevision(c_int(dev_id),byref(buff))
        res=self.get_error(resdll)
        if res!="OK":
            return "Cannot get hardware revision of device "+str(dev_id)+", error: "+res, dev_info
        dev_info["hwrev"]=str(buff.value)

        #Get firmware revision
        buff=c_ubyte() #This is the address of the variable where the hardware revision number is stored
        resdll=self.dll_handler.DcIc_GetFWRevision(c_int(dev_id),byref(buff))
        res=self.get_error(resdll)
        if res!="OK":
            return "Cannot get firmware revision of device "+str(dev_id)+", error: "+res, dev_info
        dev_info["fwrev"]=str(buff.value)

        #Get gain of sensor (sensitivity)
        buff=c_bool()
        resdll=self.dll_handler.DcIc_GetSensitivity(c_int(dev_id),byref(buff))
        res=self.get_error(resdll)
        if res!="OK":
            return "Cannot get sensitivity of detector "+str(dev_id)+", error: "+res, dev_info
        dev_info["gain_detector"]="Low" if buff.value else "High"

        #Get the gain of the camera (A/D converter mounted on the camera)
        buff=c_ubyte()
        resdll=self.dll_handler.DcIc_GetGain(c_int(dev_id),byref(buff))
        res=self.get_error(resdll)
        if res!="OK":
            return "Cannot get gain of the camera AD converter "+str(dev_id)+", error: "+res, dev_info
        dev_info["gain_roe"]="Low" if buff.value else "High"

        #Get the offset of the camera (A/D converter mounted on the camera)
        buff=c_ushort()
        resdll=self.dll_handler.DcIc_GetOffset(c_int(dev_id),byref(buff))
        res=self.get_error(resdll)
        if res!="OK":
            return "Cannot get offset of the camera AD converter "+str(dev_id)+", error: "+res, dev_info
        dev_info["offset_roe"]=buff.value

        return res, dev_info

    #---Auxiliary functions (used by the Main functions)---

    #Auxiliary functions to set the IT:
    def compute_sensor_it_min(self,clock_frequency_mhz=10.0,sensor="S13496"):
        """
        Computes the minimum integration time allowed by the sensor.
        parameters:
        <clock_frequency_mhz>: Internal clock frequency of the camera in MHz (float)
        <sensor>: Name of the sensor (string)
        """
        f_clk=clock_frequency_mhz*1.0e6  # Convert MHz to Hz
        thp_min_clk=detectors[sensor]["thp_st_min"]
        it_min_ms=(thp_min_clk+detectors[sensor]["it_offset_clk"])/f_clk*1000  # Convert clk -> s -> ms
        return it_min_ms, thp_min_clk

    def compute_camera_it_min(self,clock_frequency_mhz,camera="C13015-01",sensor="S13496"):
        """
        Computes the minimum integration time allowed by the camera.
        params:
         <clock_frequency_mhz>: Internal clock frequency of the camera in MHz (float)
         <camera>: Camera model name of the camera (string) (See cameras global variable)
         <sensor>: Sensor model name (string). (See sensors global variable)
        return:
            <it_min_ms>: Minimum integration time in milliseconds (float) allowed by the camera roe, for a particular sensor
            <thp_min_clk>: Minimum integration time in clock pulses (integer) allowed by the camera roe.
        """
        f_clk=clock_frequency_mhz*1.0e6  # Convert MHz to Hz
        thp_min_clk=cameras[camera]["thp_st_min"]  # Minimum integration time in clock pulses
        it_min_ms=(thp_min_clk+detectors[sensor]["it_offset_clk"])/f_clk*1000  # Convert clk -> s -> ms
        return it_min_ms, thp_min_clk

    def compute_st_pulses(self,it_ms,clock_frequency_mhz=10.0,camera="C13015-01",sensor="S13496"):
        """
        Computes the high period of the ST signal and the line cycle to be sent to the camera, in CLK pulses,
        in order to have a given integration time <it_ms> set into the sensor.

        Note that each device (camera and detector) have its own limits, so this function ensures that the
        integration time to be set is compatible with both.

        params:
         - <it_ms>: Integration time in milliseconds (float)
         - <clock_frequency_mhz>: Internal clock frequency of the camera in MHz (float)
         - <camera>: Camera model name of the camera (string) (See cameras global variable)
         - <sensor>: Sensor model name (string). (See sensors global variable)

        return:
            - <res>: string with the result of the operation. It can be "OK" or an error description.

        """
        res=high_period_res=low_period_res=line_cycle_res="OK"

        # Convert input values to Hz and seconds
        f_clk=clock_frequency_mhz*1.0e6  # Convert MHz to Hz
        integration_time_s=float(it_ms)/1000.0  # Convert ms to s

        # Compute the high period of ST signal (thp(ST)) in clock pulses, from the sensor point of view.
        high_period=int(round(integration_time_s*f_clk))-int(detectors[sensor]["it_offset_clk"]) #[CLK]

        #Chek minimum thp allowed by the camera (which is usually larger than the minimum of the sensor):
        if high_period<cameras[camera]["thp_st_min"]:
            high_period_res="limited by min value allowed of camera ("+str(high_period)+"->"+str(cameras[camera]["thp_st_min"])+")"
            high_period=cameras[camera]["thp_st_min"]
        #Check minimum thp allowed by the sensor:
        if high_period<detectors[sensor]["thp_st_min"]:
            high_period_res="limited by min value allowed of sensor ("+str(high_period)+"->"+str(detectors[sensor]["thp_st_min"])+")"
            high_period=detectors[sensor]["thp_st_min"]

        # Compute the -preliminary- low period of ST signal (tlp(ST)), from the sensor point of view. (the minimum allowed one)
        low_period=int(detectors[sensor]["tlp_st_min"]) #[CLK]

        # Line cycle
        line_cycle = high_period + low_period


        #Check min and max Line cycle allowed by camera:
        if line_cycle<cameras[camera]["tpi_st_min"][sensor]:
            line_cycle_res="limited by min tpi_st allowed of camera"+" ("+str(line_cycle)+"->"+str(cameras[camera]["tpi_st_min"][sensor])+")"
            line_cycle=cameras[camera]["tpi_st_min"][sensor]
        elif line_cycle>cameras[camera]["tpi_st_max"]:
            line_cycle_res="limited by max tpi_st allowed of camera"+" ("+str(line_cycle)+"->"+str(cameras[camera]["tpi_st_max"])+")"
            line_cycle=cameras[camera]["tpi_st_max"]
        #Check minimum Line cycle allowed by sensor:
        if line_cycle<detectors[sensor]["tpi_st_min"]:
            line_cycle_res="limited by min tpi_st allowed of sensor"+" ("+str(line_cycle)+"->"+str(detectors[sensor]["tpi_st_min"])+")"
            line_cycle=detectors[sensor]["tpi_st_min"]

        #Recalculate -final- low period
        low_period=line_cycle-high_period

        #Check minimum tlp allowed by the camera:
        if low_period<cameras[camera]["tlp_st_min"]:
            low_period_res="limited by min value allowed of camera ("+str(low_period)+"->"+str(cameras[camera]["tlp_st_min"])+")"
            low_period=cameras[camera]["tlp_st_min"]

        #if debug mode: info about limits.
        if self.debug_mode>2:

            inf="line_cycle: "+str(line_cycle)+" CLK"
            if line_cycle_res!="OK":
                inf+=", "+line_cycle_res
            self.logger.debug(inf)

            inf="high_period: "+str(high_period)+" CLK"
            if high_period_res!="OK":
                inf+=", "+high_period_res
            self.logger.debug(inf)

            inf="low_period: "+str(low_period)+" CLK"
            if low_period_res!="OK":
                inf+=", "+low_period_res
            self.logger.debug(inf)

        #If the high period had to be adjusted by the camera or sensor limits, return an error to indicate
        #that the high period could not be set for the requested it_ms.
        if high_period_res!="OK":
            res="NOK"

        return res,high_period,low_period,line_cycle


    #Auxiliary functions related to connection/disconnection:

    def get_error(self,resdll):
        """
        res=get_error(resdll)
        Get the error description of an unsuccessful dll return answer.

        params:
            <resdll>: returned answer from a dll call (integer or boolean)

        return:
            <res>: error description (string)
        """
        #Save last generated error code (dll answer):
        self.last_errcode=resdll

        #Check dll answer meaning
        if isinstance(resdll, int) and resdll:
            return "OK"
        else:
            #Wrong answer, try to get error description
            if self.dll_handler is not None:
                try:
                    #Get last error code from the dll handler
                    errcode=self.dll_handler.DcIc_GetLastError()
                    if errcode in self.errors:
                        res=self.errors[errcode]
                    else:
                        res="Unknown error"
                except Exception as e:
                    res="Exception while reading the last error for resdll = "+str(resdll)+": "+str(e)
            else:
                res = "Cannot check error, none dll handler to use."
        return res

    def load_spec_dll(self):
        """
        res=load_dll()
        Load the spectrometer control dll, and store the handler into dll_handler

        returns
        <res>: string with the result of the operation. It can be "OK" or an error description.
        """
        res="OK"
        self.logger.info("Loading dll: "+self.dll_path)

        #Check if file exists:
        if not os.path.exists(self.dll_path):
            res="The dll file does not exist: "+self.dll_path
            self.logger.error(res)
            return res

        try:
            if self.dll_handler is None:
                self.dll_handler = windll.LoadLibrary(self.dll_path)
            #Note: Python knows if a dll has been loaded before or not: When loading an already loaded dll,
            #it returns the same memory address of the already loaded dll.
            if self.debug_mode>0:
                self.logger.debug("dll_handler: "+str(self.dll_handler))
        except Exception as e:
            res="Exception happened while loading the dll: "+str(e)
            self.logger.exception(e)

        return res

    def set_gain_roe(self, gain="Low"):
        """
        Set the gain of the AD converter of the camera roe
        params:
            <gain> can be "Low" or "High"
        """
        if gain.upper()=="LOW":
            value=True
        elif gain.upper()=="HIGH":
            value=False
        else:
            return "Invalid gain value: "+gain
        resdll=self.dll_handler.DcIc_SetGain(self.spec_id, c_ubyte(value))
        #Inverse logic: The parameter passed is "bLow" so if a True is sent, Low gain is set.
        res=self.get_error(resdll)
        if res!="OK":
            res="Cannot set roe AD converter gain to "+gain+", error: "+res
            self.logger.warning(res)
        else:
            self.gain_roe=int(not value)
            self.logger.info("Roe AD converter gain set to "+gain)
        return res

    def set_gain_detector(self, gain="Low"):
        """
        Set the gain of the detector
        params:
            <gain> can be "Low" or "High"
        """
        if gain.upper()=="LOW":
            value=True
        elif gain.upper()=="HIGH":
            value=False
        else:
            return "Invalid gain value: "+gain
        resdll=self.dll_handler.DcIc_SetSensitivity(self.spec_id, c_bool(value))
        #Inverse logic: The parameter passed is "bLow" so if a True is sent, Low gain is set.
        res=self.get_error(resdll)
        if res!="OK":
            res="Cannot set detector gain to "+gain+", error: "+res
            self.logger.warning(res)
        else:
            self.gain_detector=int(not value)
            self.logger.info("Detector gain set to "+gain)
        return res

    def set_offset_camera(self, offset):
        """
        Set the offset of the camera AD converter.
        """
        resdll=self.dll_handler.DcIc_SetOffset(self.spec_id, c_ushort(offset))
        res=self.get_error(resdll)
        if res!="OK":
            res="Cannot set camera AD converter offset to "+str(offset)+", error: "+res
            self.logger.warning(res)
        else:
            self.offset_roe=offset
            self.logger.info("Camera offset set to "+str(offset))
        return res


    #Auxiliary functions related to measurements
    def reset_spec_data(self):
        """
        This function is called at the start of every new measurement.
        """

        #Ensure read_data_queue and handle_data_queue are empty before start using them.
        while not self.read_data_queue.empty():
            _=self.read_data_queue.get()

        while not self.handle_data_queue.empty():
            _=self.handle_data_queue.get()

        self.ncy_read=0 #Current number of cycles measured and read from the spectrometer roe
        self.ncy_handled=0 #Current number of cycles handled
        self.ncy_saturated=0 #Number of saturated cycles. (only active pixels checked)
        self.sy=np.zeros(self.npix_active,dtype=np.float64) #Sum of the counts
        self.syy=np.zeros(self.npix_active,dtype=np.float64) #Sum of the squared counts
        self.sxy=np.zeros(self.npix_active,dtype=np.float64) #Sum of the meas index by the counts
        self.arrival_times=[] #List of arrival times of the measurements
        self.meas_start_time=0 #Unix time in seconds when the measurement started
        self.meas_end_time=0 #Unix time in seconds when the measurement ended (data arrival time of the last measured cycle)
        self.data_handling_end_time=0 #Unix time in seconds when the data handling ended (all cycles received + handled + final data handling)

    def measure_pack(self,ncy_pack):
        """
        res,data,arrival_time=measure_pack(ncy_pack)
        Send an order to the spectrometer to measure a pack of ncy cycles and return the measured raw data and
        the arrival time of the pack of cycles.
        """
        #Total Number of pixel readings to be done by the dll
        npix_pack = ncy_pack * self.npix_vert * self.npix_active

        if self.simulation_mode:
            simulated_duration = self.simudur * (ncy_pack * (self.it_ms / 1000.0))  # in seconds
            #Simulated duration is reduced by simudur factor, for faster simulations
            sleep(simulated_duration)
            simulated_data = np.random.randint(int(self.simulated_rc_min/self.discriminator_factor),
                                               int(self.simulated_rc_max/self.discriminator_factor),
                                               (npix_pack,))
            arrival_time=spec_clock.now()
            return "OK", simulated_data, arrival_time

        _ = self.abort(ignore_errors=True,log=False,disable_docatch=False) #This is in theory not needed,
        # but if it is not here then the wait function returns a timeout error every X calls.

        # Prepare buffer for actual measurement
        meas_buff = (c_ushort * npix_pack)() #C array of WORD (c_ushort in Python)
        meas_buff_len_bytes = c_uint(npix_pack * 2)  # 2 bytes per pixel

        # Start measurement
        resdll = self.dll_handler.DcIc_Capture(self.spec_id, byref(meas_buff), meas_buff_len_bytes)
        res = self.get_error(resdll)
        if res != "OK":
            return "Could not start measurement, "+res+".", None, None

        # Wait for measurement to complete
        while True:

            if not self.docatch:
                #ie when saturation has been detected while handling previously
                # captured data and abort if saturation option is enabled.
                # Abort measurement (do not wait anymore because new data is going to be discarded)
                _ = self.abort(ignore_errors=True,log=False,disable_docatch=False)
                return "Measurement has been aborted.", None, None

            status = self.dll_handler.DcIc_Wait(self.spec_id)
            if status == 2:  # Measurement completed
                arrival_time=spec_clock.now()
                return "OK", meas_buff, arrival_time
            elif status == 0:  # Error occurred
                #send empty error to get_error(), to make this function to query the last generated dll error.
                res=self.get_error("")
                res="Error happen while waiting for data, "+res+"."
                return res, None, None
            elif status == 1:  # Still measuring
                sleep((self.it_ms/10.0) / 1000.0)  # Wait 10% of the integration time, ie 10ms if IT=100ms

    def data_arrival_watchdog(self):
        """
        This function will run permanently in a side thread, and is constantly waiting to have
        something (ncy) in the self.read_data_queue.

        As soon as it gets something, it will proceed to measure and get the data from the spectrometer,
        and put this data into the self.handle_data_queue.

        When disconnecting the spectrometer, the disconnect function will send a "None" to the self.read_data_queue,
        to signal that the data arrival watchdog thread must be finished.
        """
        self.logger.info("Started data arrival watchdog..")

        #Ensure read_data_queue is empty before start using it.
        while not self.read_data_queue.empty():
            _=self.read_data_queue.get()

        #Start infinite data arrival monitoring loop:
        while True:
            ncy=self.read_data_queue.get() #Blocking call
            if ncy is None: #Exit flag
                self.logger.info("Exiting data arrival watchdog thread...")
                break
            else: #Normal data arrival -> measure and get the data from the spectrometer:
                res=self.measure_blocking(ncy)

                #update last error:
                self.error=res

                #If error, signal that the measurement is "complete" to unblock the main thread in case of a measure_blocking call:
                if res!="OK":
                    self.internal_meas_done_event.set()
        self.logger.info("Exiting data arrival watchdog")

    def data_handling_watchdog(self):
        """
        This function will run permanently in a side thread, and is constantly waiting to have something in the
        self.handle_data_queue.
        As soon as it gets something, it will proceed to handle the data, and check if the measurement is complete.
        If the measurement is complete, it will set the self.internal_meas_done_event, which will indicate
        the measure_blocking call that all requested cycles have been handled, and the measurement can be
        considered as finished.
        """

        #Ensure data_handling_queue is empty before start using it.
        while not self.handle_data_queue.empty():
            _=self.handle_data_queue.get()

        #Start the infinite data handling loop:
        while True:
            (call_index,arrival_time,(rc,rc_blind_left,rc_blind_right))=self.handle_data_queue.get()
            if call_index is None: #Exit flag -> data arrival watchdog thread must be finished.
                self.logger.info("Exiting data handling watchdog thread of spectrometer "+self.alias+"...")
                break
            elif not self.docatch:
                continue #ignore data
            else: #Normal data arrival -> handle cycle data:
                #Append the arrival time to the "effective" arrival times, which only contains
                #the arrival times of the effectively handled cycles/pack of cycles.
                self.arrival_times.append(arrival_time)
                #Convert ctypes array to numpy array
                rc = np.ctypeslib.as_array(rc) #1D array of shape npix_call (=ncy_call*npix_tot)
                rc = rc.astype(np.float64)
                ncy_pack=self.ncy_per_meas[call_index]
                rc = np.split(rc, ncy_pack) #Split the long 1D array in ncy_pack parts -> list of rc arrays,
                #each one contains the data of one cycle
                for i in range(len(rc)): #handle each cycle data

                    self.ncy_read+=1

                    issat, data_ok=self.handle_cycle_data(self.ncy_read,rc[i],rc_blind_left,rc_blind_right)
                    #handle_cycle_data will update self.ncy_handled

                    if (issat and self.abort_on_saturation) or not data_ok:

                        if issat:
                            self.logger.info("data_handling_watchdog, saturation detected in spec "+self.alias+
                                             ", for ncy read ="+str(self.ncy_read)+"/"+str(self.ncy_requested)+
                                             ". Aborting due to saturation...")

                        if not data_ok:
                            self.logger.warning("data_handling_watchdog, data not consistent in spec "+self.alias+
                                                ", for nmeas read ="+str(call_index)+"/"+str(self.ncy_requested)+
                                                ". Aborting due to inconsistent data (As if it were saturated)")

                        self.docatch=False #Stop capturing / handling more data from now on.

                        #discard any data in the handle_data_queue -> no more data will be handled:
                        while not self.handle_data_queue.empty():
                            _=self.handle_data_queue.get()

                        #Finish the measurement:
                        self.measurement_done()

                        break #quit handle cycles loop
                    else:

                        if self.debug_mode>=3:
                            self.logger.debug("data_handling_watchdog, ncy handled="+ \
                                              str(self.ncy_handled)+"/"+str(self.ncy_requested))

                        #If measurement is completed without saturation or ignoring saturation:
                        if self.ncy_handled==self.ncy_requested:
                            self.measurement_done()
                            break #quit handle cycles loop

    def handle_cycle_data(self,ncy_read,rc,rc_blind_left,rc_blind_right):
        """
        Handle the measurement cycle data
        This function will be called by the data handling watchdog, when a measurement has been already read from the
        spectrometer, and is ready to be handled.
        This function applies any needed post-processing to the measurement data, to have the raw data converted into
        the proper units [counts]. Then it is checked if the data is saturated, and if so, the saturated_meas_counter
        is incremented. It also checks for data consistency, detecting negative or NaN values.
        Finally, the data is accumulated in the sy, syy, and sxy variables, that are used to calculate the mean and
        standard deviation later, at the final data handling step.

        params:
            <ncy_read>: read measurement number (from 1 to requested_nmeas)
            <rc>: raw counts of the active pixels (np array)
            <rc_blind_left>: raw counts of the blind pixels on the left side of the detector (if any) (np array)
            <rc_blind_right>: raw counts of the blind pixels on the right side of the detector (if any) (np array)

        returns:
            <issat>: boolean, True if the last handled data is saturated, False otherwise.
            <data_ok>: boolean, True if the data is consistent (no negative values, no NaN), False otherwise.
        """
        cycle_index=ncy_read-1 #0-based index of the cycle being handled

        #Apply discriminator factor:
        if self.discriminator_factor!=1:
            rc=rc*float(self.discriminator_factor)

        rcmax=rc.max()
        rcmin=rc.min()

        #Consistency check of the data. (eg. all elements >0, no nans, etc.)
        if rcmin<0:
            self.logger.warning("handle_cycle_data, negative counts detected !!!")
            data_ok=False
        elif np.isnan(rcmax) or np.isnan(rcmin):
            self.logger.warning("handle_cycle_data, NaN counts detected !!!")
            data_ok=False
        else:
            data_ok=True

        #Detect saturation:
        issat = rcmax>=self.eff_saturation_limit
        if (issat and self.abort_on_saturation) or not data_ok:
            #Do not add cycle data to accumulated data in this case.
            #This cycle data won't be used.
            #(in the accumulated data there won't be any saturated cycle)
            return issat, data_ok #-> Quit

        else: #Continue even if saturation is detected:

            #Add cycle data to accumulated data for active pixels:
            self.sy=self.sy+rc
            self.syy=self.syy+rc**2
            self.sxy=self.sxy+cycle_index*rc

            # #Do the same for blind pixels (if any):
            # if len(rc_blind_left)>0:
            #     #Convert to float64
            #     rc_blind_left=rc_blind_left.astype(np.float64)
            #     #Apply discriminator factor:
            #     if self.discriminator_factor!=1:
            #         rc_blind_left=rc_blind_left*float(self.discriminator_factor)
            #     #Add data to accumulated data:
            #     self.sy_blind_left=self.sy_blind_left+rc_blind_left
            #     self.syy_blind_left=self.syy_blind_left+rc_blind_left**2
            #     self.sxy_blind_left=self.sxy_blind_left+cycle_index*rc_blind_left
            #
            # if len(rc_blind_right)>0:
            #     #Convert to float64
            #     rc_blind_right=rc_blind_right.astype(np.float64)
            #     #Apply discriminator factor:
            #     if self.discriminator_factor!=1:
            #         rc_blind_right=rc_blind_right*float(self.discriminator_factor)
            #     #Add data to accumulated data:
            #     self.sy_blind_right=self.sy_blind_right+rc_blind_right
            #     self.syy_blind_right=self.syy_blind_right+rc_blind_right**2
            #     self.sxy_blind_right=self.sxy_blind_right+cycle_index*rc_blind_right

            self.ncy_handled+=1
            if issat:
                self.ncy_saturated+=1

        return issat, data_ok

    def measurement_done(self):
        """
        Final actions to be done when a measurement is complete.
        """
        self.meas_end_time=self.arrival_times[-1] #Unix Time in which the spectrometer indicated to the pc
        # that the last cycle (pack of cycles) was finished, and it was ready to be read.
        #Calculate mean, standard deviation and rms to a fitted straight line (for active pixels):
        x=np.arange(self.ncy_handled)
        res,self.rcm,self.rcs,self.rcl=calc_msl(self.alias,x,self.sxy,self.sy,self.syy)
        if res!="OK":
            self.logger.warning("Error at function calc_msl: "+res)

        if self.debug_mode>=1:
            self.logger.debug("Measurement done for spec "+self.alias)

        self.data_handling_end_time=spec_clock.now()

        #Signal that the measurement is complete (internally)
        #This event will unblock any eventual measure_blocking function
        #when it is waiting to have all data handled
        self.internal_meas_done_event.set()

    #Auxiliary functions related to performance & statistics

    def performance_test(self,fpath=""):
        """
        res,presults = performance_test(fpath)

        This function performs a certain number of sequential measurements at different integration times,
        and different number of cycles, and calculates the performance statistics for each of them.
        The results are stored into a txt file, where the first line is the header, and the following columns contains:
        -the first column contains the integration times,
        -the second column contains the number of cycles,
        -the third column contains the real measurements duration (discarding the cycle handling time),
        -the fourth column contains the "mean" calculated cycle delay time (cdt_mean) for the respective integration time and number of cycles,
          calculated as cdt_mean=(Real_dur_measurements-ncy*IT)/ncy
        -the fifth column contains the "median" cycle delay time (cdt_median), calculated as
          cdt_median=((median delta of data arrival events)-IT). This is only possible when we have a data arrival time for
          every measured cycle. If not, the cdt_median will be equal to cdt_mean.

        params:
            <fpath>: (string) Path to the folder where the results file will be stored.
        returns:
            <res>: (string) "OK" if the performance test was successful, or an error description if not.
            <presults>: (np array) 2D array with the performance results.

        """
        res="OK"
        presults=np.array([])
        self.logger.info("Doing performance test - analyzing cycle delay time behavior with respect ncy and IT")
        its=[]
        ncys=[]
        real_dur_meass=[]
        cdts_mean=[]
        cdts_median=[]

        test_start_time=spec_clock.now()

        for it in self.performance_test_it_ms_list:
            it=round(it,3) #avoid numerical errors
            self.set_it(it_ms=it)
            sleep(it*1e-3) #ms->s (Wait to have new IT applied)
            for ncy in self.performance_test_ncy_list:
                ncy=int(ncy)
                res=self.measure_blocking(ncy=ncy)
                if res!="OK":
                    break
                cdt_mean,cdt_median,real_dur_meas,_,_,_=self.calc_performance_stats(showinfo=False) #get cycle delay time
                its.append(it)
                ncys.append(ncy)
                real_dur_meass.append(real_dur_meas)
                cdts_mean.append(cdt_mean)
                cdts_median.append(cdt_median)
                self.logger.info("IT="+str(it)+" ms, ncy="+str(ncy)+", cdt_mean="+str(cdt_mean)+" ms/cy, cdt_median="+str(cdt_median)+" ms/cy")

        if res=="OK":

            test_end_time=spec_clock.now()
            test_duration=test_end_time-test_start_time #s
            self.logger.info("Performance test duration: "+str(test_duration)+" s")

            #Put the performance results all together into a 2D np array, and write it into a file:
            presults=np.array([its,ncys,real_dur_meass,cdts_mean,cdts_median]).T
            header="IT[ms]; ncy; real_dur[ms]; cdt_mean[ms/cy]; cdt_median[ms/cy]"
            ptest_filename=self.sn+"_performance_test.txt"
            ptest_filepath=os.path.join(fpath,ptest_filename)
            with open(ptest_filepath,"w") as f:
                np.savetxt(f,presults,fmt="%.4f",delimiter=";",header=header,comments="#")

        if res=="OK":
            self.logger.info("Finished performance test")
        else:
            res="Error during performance test: "+res
            self.logger.error(res)

        self.error=res
        return res,presults

    def calc_performance_stats(self,showinfo=True):
        """
        Calculate the performance statistics of the last measurement.
        -Measurements duration
        -Final data handling duration
        -Total duration
        -Determination of the mean and median cycle delay time of the last measurement.
        """
        real_dur_meas=1000.0*(self.meas_end_time-self.meas_start_time) #real duration of measurements [ms]
        real_dur_fdh=1000.0*(self.data_handling_end_time-self.meas_end_time) #real duration of the final data handling, [ms]
        real_dur_total=real_dur_meas+real_dur_fdh # real total duration (measurements + final data handling) [ms]
        expected_min_dur_meas=self.ncy_requested*self.it_ms #expected minimum duration of the measurements, [ms]. (=ncy*IT)
        #expected_min_dur_fdh=0 #expected minimum duration of final data handling, [ms]
        #expected_min_dur_tot=expected_min_dur_meas+expected_min_dur_fdh #expected minimum total duration, [ms]
        cdt_mean=max(0,(real_dur_meas-expected_min_dur_meas)/self.ncy_requested) #Mean cycle delay time of last measurement [ms/cy]
        deltas_min=np.nan
        deltas_max=np.nan

        #Calc the median cycle delay time (will be used to estimate the duration of a future measurement, as dur=ncy*(IT+cdt_median))
        #4 possible cases:
        #1) Measured ncy cycles one by one (self.max_ncy_per_meas==1), where only 1 cycle was measured (len(self.arrival_times)==1)
        #2) Measured ncy cycles one by one (self.max_ncy_per_meas==1), where more than 1 cycle was measured (len(self.arrival_times)>1)
        #3) Measured ncy cycles in X packs of Y cycles (self.max_ncy_per_meas>1), where only 1 pack was needed (len(self.arrival_times)==1)
        #4) Measured ncy cycles in X packs of Y cycles (self.max_ncy_per_meas>1), where more than 1 pack was needed (len(self.arrival_times)>1)

        if self.max_ncy_per_meas==1: #Measured ncy cycles one by one.
            if len(self.arrival_times)==1: #Only one cycle was measured
                case=1
                cdt_median=cdt_mean
            else:
                case=2
                #We have a data arrival time for each cycle, so we can calculate the deltas between them.
                #By analyzing the deltas in the data arrival times, we can discard eventual outliers, (cycles arriving later than expected, eventually).
                #So we can calculate a more robust "future cycle delay time" by using the median of the data arrival time deltas, minus the integration time.
                deltas=1000.0*np.diff(self.arrival_times) #s->ms #delta of data arrival events (ddae)
                deltas_mean=np.mean(deltas) #mean of the deltas
                deltas_median=np.median(deltas) #median of the deltas
                deltas_max=np.max(deltas) #max of the deltas
                deltas_min=np.min(deltas) #min of the deltas
                stdev=np.std(deltas) #stdev of the deltas
                cdt_median=deltas_median-self.it_ms #"median" cycle delay time
                #we use the median instead of the mean to avoid eventual outliers.

        else: #Measured ncy cycles in X packs of Y cycles

            if len(self.arrival_times)==1: #Only one pack was measured
                case=3
                #We have only one data arrival time for all measured cycles.
                #So we cannot calculate the deltas between the data arrivals of each cycle.
                #We can only calculate the "future cycle delay time" by using the difference between the expected duration and the real duration.
                cdt_median=cdt_mean

            else: #Multiple packs were needed.
                case=4
                #We have multiple data arrival times for each "pack" of measured cycles.
                # We can calculate the cycle delay time of each pack of cycles, and then return the median of them.
                cdts = []
                prev_time = self.meas_start_time
                for ncy_pack, arrival in zip(self.ncy_per_meas, self.arrival_times): #number of cycles requested per meas call, arrival_times
                    pack_dur = 1000.0*(arrival - prev_time) #s ->ms
                    expected_pack_dur = ncy_pack * self.it_ms
                    cdt = max(0, (pack_dur - expected_pack_dur) / ncy_pack)  # mean cycle delay time of this pack [ms/cy]
                    cdts.append(cdt)
                    prev_time = arrival

                # Calculate median cdt
                cdt_packs_max=np.max(cdts)
                cdt_packs_min=np.min(cdts)
                cdt_packs_mean=np.mean(cdts)
                cdt_packs_std=np.std(cdts)
                cdt_packs_median=np.median(cdts)

                cdt_median = cdt_packs_median


        #Show results:
        if showinfo:
            self.logger.info("---Spec "+self.alias+" last measurement stats:---")
            self.logger.info("measured "+str(self.ncy_read) + " cycles at IT="+str(self.it_ms)+" ms")
            self.logger.info("Real_dur(meas)="+str(real_dur_meas)+"ms, Real_dur(final data handling)="+str(real_dur_fdh)+"ms, Real_dur(total)="+str(real_dur_total)+"ms")
            self.logger.info("Expected_min_dur(meas) (=ncy*IT) = "+str(expected_min_dur_meas)+"ms")
            self.logger.info("Measurement_delay = Real_dur(meas) - Expected_min_dur(meas) = "+str(real_dur_meas - expected_min_dur_meas)+"ms")
            self.logger.info("Mean cycle delay time: cdt_mean=max(0,Measurement_delay/ncy) = "+str(cdt_mean)+"ms/cy")
            if case in [1,3]:
                self.logger.info("The last measurement cycle delay time has been determined as (=cdt_mean)="+str(cdt_median)+"ms/cy")
            elif case == 2:
                self.logger.info("delta of data arrival events (ddae), last 5: "+str(deltas[-5:])+" ms") #last 5
                self.logger.info("delta of data arrival events: ddae_mean="+str(deltas_mean)+", ddae_median="+str(deltas_median)+", ddae_max="+str(deltas_max)+", ddae_min="+str(deltas_min)+", ddae_std="+str(stdev)+" [ms] (IT="+str(self.it_ms)+" ms)")
                self.logger.info("The last measurement cycle delay time has been determined as max(0,ddae_median("+str(deltas_median)+"ms)-IT("+str(self.it_ms)+"ms))="+str(cdt_median)+"ms/cy")
            elif case == 4:
                self.logger.info("mean cycle delay time of the last 5 packs of cycles (cdt_packs[-5:]): "+str(cdts[-5:])+" ms/cy") #last 5
                self.logger.info("cdt_packs: mean="+str(cdt_packs_mean)+", median="+str(cdt_packs_median)+", max="+str(cdt_packs_max)+", min="+str(cdt_packs_min)+", std="+str(cdt_packs_std)+" [ms/cy]")
                self.logger.info("The last measurement cycle delay time has been determined as (=cdt_packs_median)="+str(cdt_median)+"ms/cy")
            self.logger.info("--------")

        return cdt_mean,cdt_median,real_dur_meas,real_dur_fdh,deltas_max,deltas_min

    #Auxiliary functions for recovery:

    def reset_device(self):
        """
        Perform a hard reset on the given spectrometer
        (microprocessor and USB controller will be reset).
        Note: The spec will start its reset procedure right after sending the command response back to the host.
        So it is needed to wait a bit after the response is received, to have the spec and usb properly initialized.

        Note that this will reload the all parameters from EEPROM.
        """
        self.logger.info("Resetting spectrometer device "+self.alias+"...")

        resdll=self.dll_handler.DcIc_Reset(self.spec_id)
        res=self.get_error(resdll)
        if res!="OK":
            res="Could not reset spectrometer "+self.alias+". Error: "+res
            self.logger.error("reset_device, "+res)
        else:
            sleep(5)

        self.error=res
        return res

    def test_recovery(self):
        """
        Test the recovery function of the spectrometer after a communication failure.

        This function will order a measurement, then it will propose the user to unplug and plug the spectrometer USB from the PC,
        and then it will try to recover the spectrometer communication.
        """
        self.logger.info("Testing recovery of spectrometer "+self.alias)

        #Set a large integration time
        res=self.set_it(it_ms=8000.0)

        #Measure (non-blocking call):
        if res=="OK":
            self.measure(ncy=1)
            self.docatch=False #-> Ignore new data arrivals on purpose.

        #Emulate a spectrometer connection/power loss
        if res=="OK":
            j=10
            while j>0:
                self.logger.info("------>  Unplug & Plug the spectrometer "+self.alias+" ("+self.sn+") USB from the PC now. ("+str(j)+"/10s) <------")
                j-=1
                sleep(1)

        #Start soft recovery:
        if res=="OK":
            res=self.recovery()

        #Finally, try to do a short testing measurement to check if the recovery was successful:
        if res=="OK":
            res=self.set_it(it_ms=100.0)
            if res=="OK":
                res=self.measure(ncy=1)
            if res=="OK":
                #wait for the measurement to be completed:
                res=self.wait_for_measurement()

        #Inform if the measurement was successful or not:
        if res=="OK":
            self.logger.info("Recovery test of spectrometer "+self.alias+" was successful.")
        else:
            self.logger.error("Recovery test of spectrometer "+self.alias+" failed. res="+res)

        return res









if __name__ == "__main__":

    print("Testing Hama3 Spectrometer Class")

    #----Select Testing parameters----:
    #Configure DLL path (having into account if running python 32bits, or python 64bits):
    arch=platform.architecture()[0]
    if arch=="32bit":
        dll_path = os.path.abspath("../../lib/oslib/spec_hama3/DcIcUSB_v1.1.0.7/x86/DcIcUSB.dll")
    else:
        dll_path = os.path.abspath("../../lib/oslib/spec_hama3/DcIcUSB_v1.1.0.7/x64/DcIcUSB.dll")

    #Select config to be tested:
    instruments=["TestBench"]
    simulation_mode=[False]
    debug_mode=[1]
    #dll_logging=[True] #internal dll logging file
    do_recovery_test=False
    do_performance_test=False
    performance_test_fpath="C:/Temp"

    instruments=instruments[:]

    #---------Testing Code Start Here-----

    SP=[]
    for i in range(len(instruments)):
        instr=instruments[i]

        #Initialize an Avantes spectrometer instance:
        SP.append(Hama3_Spectrometer())

        #Configure instance:
        SP[i].dll_path=dll_path
        SP[i].debug_mode=debug_mode[i]
        #SP[i].dll_logging=dll_logging[i]
        SP[i].simulation_mode=simulation_mode[i]
        SP[i].alias=str(i+1)

        if instr=="TestBench":
            SP[i].sn="46AN07767" #CMOS_EVO
            SP[i].npix_active=4096
            SP[i].min_it_ms=0.006
            SP[i].performance_test_it_ms_list=np.arange(0.006,10.1,1.0)
            SP[i].performance_test_ncy_list=[1, 10, 100, 200, 500, 1000] #Number of cycles to be tested for each IT
        else:
            raise("Instrument "+instr+" not recognized.")

        #Configure the logger for the spec alias:
        SP[i].initialize_spec_logger()

    #Configure logging for the testing console:
    if np.any(debug_mode):
        loglevel=logging.DEBUG #TODO: Create independent loggers for each spectrometer
    else:
        loglevel=logging.INFO

    # logging formatter
    log_fmt="[%(asctime)s.%(msecs)03d] [%(levelname)s] [%(name)s] [%(message)s]"
    log_datefmt="%Y%m%dT%H%M%SZ"
    formatter = logging.Formatter(log_fmt, log_datefmt)

    # Configure the basic properties for logging
    currisodate=datetime.now().strftime("%Y%m%dT%H%M%SZ")
    logfile="C:/Temp/"+currisodate+"_hama3_spec_test_"+"_".join(instruments)+".txt"
    print("logging into : "+logfile)
    logging.basicConfig(level=loglevel,format=log_fmt,datefmt=log_datefmt,
                        filename=logfile)

    # Create a StreamHandler for console output
    console_handler = logging.StreamHandler()
    console_handler.setLevel(loglevel)  # Set the log level for the console handler
    console_handler.setFormatter(formatter)

    # Add the console handler to the root logger
    logging.getLogger().addHandler(console_handler)


    #---Testing actions---
    res="OK"

    #Connect the spectrometers:
    logger.info("--- Connecting spectrometers ---")
    for i in range(len(SP)):
        try:
            res=SP[i].connect()
        except Exception as e:
            logger.exception(e)
            res="Exception while connecting to spectrometer: "+str(e)

        if res!="OK":
            break

    try:

        #Set integration time:
        if res=="OK":
            logger.info("--- Setting IT ---")
            for i in range(len(SP)):
                res=SP[i].set_it(it_ms=200.0)
                if res!="OK":
                    break

        #Do testing Measurement (non-blocking call):
        if res=="OK":
            logger.info("--- Starting measurements ---")
            for i in range(len(SP)):
                res=SP[i].measure(ncy=10)

        #Wait for measurement finished (blocking call)
        if res=="OK":
            for i in range(len(SP)):
                if SP[i].measuring:
                    res=SP[i].wait_for_measurement()
            logger.info("All measurements done")

        if res=="OK":
            logger.info("Waiting 5 seconds")
            sleep(5)

        if res=="OK" and do_recovery_test:

            #Test if recovery works:
            if res=="OK":
                sleep(3)
                logger.info("--- Testing if soft-recovery works in the last connected spec ---")
                res=SP[i].test_recovery()
                sleep(1)

            #Do a final measurement test after recovery, to ensure all specs are still working after recovery:
            #Set integration time:
            if res=="OK":
                logger.info("--- Final Test - Setting IT ---")
                for i in range(len(SP)):
                    res=SP[i].set_it(it_ms=200.0)
                    if res!="OK":
                        break

            #Do testing Measurement:
            if res=="OK":
                logger.info("--- Final Test - Starting measurements ---")
                for i in range(len(SP)):
                    res=SP[i].measure(ncy=10)
                    if res!="OK":
                        break

            #Wait for measurement finished
            if res=="OK":
                for i in range(len(SP)):
                    if SP[i].measuring:
                        logger.info("--- Waiting for measurement of spectrometer "+SP[i].alias+" to finish... ---")
                        res=SP[i].wait_for_measurement()

        if res=="OK" and do_performance_test:
            for i in range(len(SP)):
                SP[i].performance_test(fpath=performance_test_fpath)
                logger.info("Performance test for spec "+str(i+1)+" finished.")

    except Exception as e:
        logger.exception(e)

    #Finally, disconnect
    logger.info("--- Test Finished, disconnecting... ---")
    sleep(2)
    for i in range(len(SP)):
        res=SP[i].disconnect(dofree=True)

    logger.info("--- Finished ---")

