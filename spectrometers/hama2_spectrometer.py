#Hamamatsu spectrometers control library for BlickO.
#It is also a standalone library (together with spec_xfus) that can be used to control Hammamatsu spectrometers
#directly (see the __main__ section at the end of the file).
#Written by Daniel Santana

from spec_xfus import spec_clock, calc_msl, split_cycles
import logging
from ctypes import c_double


import numpy as np
from time import sleep
from copy import deepcopy
import sys
import os
import threading
#from matplotlib import pyplot as plt


from spectrometers.spec_hama2.Hamamatsu_DCAMSDK4_v25056964.dcam import Dcamapi, Dcam
from spectrometers.spec_hama2.Hamamatsu_DCAMSDK4_v25056964.dcamapi4 import (
    DCAMCAP_STATUS,
    DCAMPROP,
    DCAM_IDPROP,
    DCAM_IDSTR,
)

if sys.version_info[0] < 3:
    # Python 2.x
    from Queue import Queue
else:
    # Python 3.x
    from queue import Queue


#create a module logger
logger=logging.getLogger(__name__)

#---Constants---




hama2_errors={ }

#---Structures---

#Dictionary to store the memory addresses of the Hama2_Spectrometers instantiated objects, indexed by their spec_id.
Hama2_Spectrometer_Instances = {}


spec_dll_initialized=False

class Hama2_Spectrometer():

    def __init__(self):
        self.spec_type="Hama2" #Type of spectrometer (string). Just for logging purposes

        #Note: Next to the parameters description there will be an (E) or an (I).
        # (E) means that the parameter may be eventually accessed / configured by BlickO.
        # (I) means that the parameter is for internal usage only, and won't be accessed by BlickO.

        #---Parameters---
        self.debug_mode=0 #(E) 0=disabled, >0=enabled. Low numbers are less verbose, high numbers are more verbose.
        #debug_mode=0 will disable the debug mode, only the basic connection/disconnection steps (with self.logger.info()) will be printed in the log files.
        #debug_mode=1 will print the previous plus the debug messages related to start and end of the measurements.
        #debug_mode=2 will print the previous plus the changes of integration time (set_it() function). Also the aux sensor readings.
        #debug_mode=3 will print the previous plus reset_data, read_data and handle_data actions (every cycle actions).


        self.simulation_mode=False #(E) Set this to True to enable the simulation mode (no dll will be loaded, no spectrometer will be really connected)
        #self.dll_logging=False #(E) Set this to True to enable the internal logging of the dll. (Only for debugging purposes)

        #self.dll_path="" #(E) Will store the path to the spec control dll (string)
        #Spec Parameters:
        self.sn="920QL102" #(E) Serial number of the spectrometer to be used (string)
        self.alias="1" #(E) Spectrometer index alias (string), i.e: "1" for spec1, or "2" for spec2. Just to
        # identify the spec index in the log files, for when there is more than one spectrometer connected.
        self.npix_active=2048 #(E) Number of active pixels of the detector (integer)
        self.npix_blind_left=0 #(E) Number of blind pixels at left side of the detector (integer)
        self.npix_blind_right=0 #(E) Number of blind pixels at right side of the detector (integer)
        # Note: the total number of pixels will be = npix_blind_left + npix_active
        self.nbits=16 #(E) Number of bits of the spectrometer detector A/D converter (integer)
        self.max_it_ms=4000.0 #(E) Maximum integration time allowed by the spectrometer, in milliseconds (float) (=Max exposure time)
        self.min_it_ms=0.008 #(E) Minimum integration time allowed by the spectrometer, in milliseconds (float) (=Min exposure time)
        self.discriminator_factor=1.0 #(E) Discriminator factor to be applied to the counts. Received raw counts will be multiplied by this factor. (float)
        self.contrast_gain=3 #None #(E) Contrast gain of the detector (integer or None). None = do not set at connection time.
        self.eff_saturation_limit=2**self.nbits-1 #(E) Effective saturation limit of the detector [max counts] (integer). Measurements containing counts above this limit will be considered as saturated.
        self.cycle_timeout_ms=4000 #(I) Timeout to have 1 cycle (or pack of cycles) of data ready (integer, milliseconds). If the data does not arrive after the expected arrival time + this timeout, a timeout error will be raised.
        self.spec_cooler_set_temp=None #(E)Setting temp of the spectrometer cooler (float or None). None = do not set at connection time.

        #Working mode:
        self.abort_on_saturation=True #(E) boolean - If True, the measurement will be aborted as soon as saturated signal
        # is detected in the latest cycle read data. An order to abort the rest of the measurement will be sent to the spectrometer
        # and no more data will be handled from that moment. The output data would be still usable, but it would only
        # contain the non-saturated cycles (if any).
        self.max_ncy_per_meas_default = 100  #(E) Default maximum number of cycles to measure per measurement order (integer).
        self.max_ncy_per_meas = self.max_ncy_per_meas_default  # (I) Current Maximum number of cycles to measure per measurement order (integer).
        self.max_it_ms_for_meas_pack = 1000 #(I) Integration time [ms] beyond which the max_ncy_per_meas will be forced to 1 cycle per measurement. Otherwise default value is used.

        #Performance tests:
        self.performance_test_it_ms_list=np.arange(2.4,10.1,0.1) #(I) List or Array with the different integration times to be tested during the performance test. [ms]
        self.performance_test_ncy_list=[1,10,30,50,70,90,110,130,150] #(I) List or Array with the different number of cycles to be tested during the performance test. [list of int]

        #Simulation mode parameters:
        self.simulated_rc_min=int(0.2*(2**self.nbits-1)) #Min and max raw counts to be measured in simulation mode (integer>0)
        self.simulated_rc_max=int(0.5*(2**self.nbits-1))
        self.simudur=0.1 #Duration of the simulated measurements with respect the original duration. (IT will be reduced by this factor, for faster simulations) (float)

        #--------Do not modify anything below this line, the following variables are for internal usage only----
        self.dll_handler=Dcamapi #(E) Will store the handler to the control dll
        self.spec_handler=None #(E) Will store the spectrometer handler (Dcam object)
        self.spec_id=None #(E) Will store the spectrometer id
        self.parlist=None #(E) Will be used to store the low level parameter list (internal configuration parameters of the spectrometer).
        self.it_ms=None #(E) Will store the currently set integration time in milliseconds. Use set_it() to change it.
        self.logger=None #(E) Will store the logger object for one specific spectrometer (logging.Logger object, see initialize_spec_logger())
        #self.devtype=None #(I) Will store the spectrometer device type (ROE type, ie AS5216 or AS7010) (string). Used for internal recovery protocols.

        #Internal variables for measure control
        self.measuring=False #(E) Will be used to indicate that the spectrometer is measuring (boolean)
        self.recovering=False #(E) Will be used to indicate that the spectrometer is in recovery mode (boolean) -> it will disable the external_meas_done_event
        self.docatch=False #(E) Will be used to "hear" for data arrival events. If False, the data arrival events will be ignored.
        self.busy=False #(E) Will be used to know when the spectrometer dll is busy (boolean) -> currently only used in read_data, in order not to send an stop measure order (due saturation) while the dll is reading data.
        self.ncy_requested=0 #(E) Will store the number of cycles requested (integer)
        self.ncy_per_meas = [1] #(I) Depending on max_ncy_per_meas, the cycles will be measured in packs. This list of integers will store how many cycles are going to be requested in each measurement call. I.e: if max_ncy_per_meas=10 and ncy_requested=21, then ncy_per_meas=[10,10,1]
        self.ncy_read=0 #(E) Will store the current number of measurements done (integer)
        self.ncy_saturated=0 #(E) Will store how many saturated measurements there are in the handled data (integer)
        self.internal_meas_done_event=threading.Event() #(I) This internal event will be "unset" whenever a measurement is started, and "set" when the measurement is complete (all ncy read and handled). Its usage is internal: just for this module.
        self.line_bundle_height=None #(I) Will store the currently configured line bundle height (integer or None). None = not set yet.

        #Internal variables for data output:
        self.rcm=np.array([]) #(E) Will store the mean raw counts of the measurements (numpy array)
        self.rcs=np.array([]) #(E) Will store the (sample) standard deviation of the raw counts of the measurements (numpy array)
        self.rcl=np.array([]) #(E) Will store the rms of the standard deviation fitted to a straight line
        self.rcm_blind_left=np.array([]) #(E) same as rcm, but for the blind pixels at the left side of the detector
        self.rcs_blind_left=np.array([]) #(E) same as rcs, but for the blind pixels at the left side of the detector
        self.rcl_blind_left=np.array([]) #(E) same as rcl, but for the blind pixels at the left side of the detector
        self.rcm_blind_right=np.array([]) #(E) same as rcm, but for the blind pixels at the right side of the detector
        self.rcs_blind_right=np.array([]) #(E) same as rcs, but for the blind pixels at the right side of the detector
        self.rcl_blind_right=np.array([]) #(E) same as rcl, but for the blind pixels at the right side of the detector

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

        #(I) Database of possible dll error codes:
        self.errors = hama2_errors

        self.error="OK" #(E) Latest generated error description (string). See self.errors for a list of possible error descriptions.
        # It can also be "OK" if no error, or "UNKNOWN_ERROR" if the dll returned an error code that is not in the list of known errors.

        self.last_errcode=0 #(I) Latest generated dll error code (integer). See self.errors for a list of possible error codes.


        #----------------------------------------------

    #---Main Control Functions (used by BlickO)---

    def initialize_spec_logger(self):
        """
        Creates a logger object for a specific instance of the spectromter class
        """
        self.logger = logging.getLogger("spec"+self.alias)

    def connect(self):
        """
        Connects to the spectrometer and initializes it.
        """

        res="OK"
        devs_info={}
        ndev=0
        npix=0

        #Reset data:
        self.reset_spec_data()

        if self.simulation_mode:
            self.logger.info("--- Connecting spectrometer "+self.alias+"... (Simulation Mode ON) ---")
            self.spec_id=len(Hama2_Spectrometer_Instances)+1 #Create a new spec_id
            Hama2_Spectrometer_Instances[self.spec_id]=self #Add this class instance to the instances dictionary, indexed by its spec_id.

        else:
            self.logger.info("--- Connecting "+str(self.spec_type)+" spectrometer "+self.alias+"... ---")

            #Load dll:
            # For this spec, it is needed to install the DCAM-API package, which includes the drivers and the API dll.
            # The API dll will be copied into C:/Windows/system32/DCAMAPI.dll as part of the installation.
            # This is the dll that will be loaded.
            res=self.load_and_init_hama2_dll()

            #Enable/disable dll debug mode:
            # if res=="OK":
            #     res=self.enable_dll_logging(enable=self.dll_logging)

            #Check number of USB connected devices is >0
            if res=="OK":
                res,ndev = self.get_number_of_devices()

            #Get device information of all connected specs
            if res=="OK":
                res,devs_info=self.get_all_devices_info(ndev)

            #Find spectrometer with correct serial number:
            if res=="OK":
                res, self.spec_id, spec_info = self.find_spec_info(devs_info)

            #Activate spectrometer:
            if res=="OK":
                res,self.spec_handler=self.get_spec_handler(self.spec_id)
                sleep(0.2)

            # Get device config
            if res=="OK":
                _=self.get_device_config() #This step is not essential, skip res if not successful

            # Get current spec cooler settings
            if res=="OK" and self.spec_cooler_set_temp is not None:
                _=self.get_cooler_settings()

            #Print device info, ROE model:
            # if res=="OK":
            #     _,self.devtype=self.get_device_type() #This step is not essential, skip res if not successful

            #Print device info: Detector model:
            # if res=="OK":
            #     _,_=self.get_detector_name() #This step is not essential, skip res if not successful

            #Print device info: FPGA version, firmware version, and dll version:
            # if res=="OK":
            #     _,_,_,_=self.get_version_info() #This step is not essential, skip res if not successful

            #Send an order to stop any ongoing measurement:
            if res=="OK":
                res=self.abort(ignore_errors=True)

            #Get number of <active> pixels:
            if res=="OK":
                res,npix=self.get_number_of_pixels()

            #Check number of pixels is correct:
            if res=="OK":
                if npix!=self.npix_active:
                    res="Number of active pixels of spec "+self.alias+" is "+str(npix)+", expected "+str(self.npix_active)+". Check IOF parameters."
                    self.logger.error("connect, "+res)

            #Check number of blind pixels is correct:
            #There is no function to check this...

            #Set device config:
            if res=="OK":
                res = self.set_device_config()


        #Set initial integration time:
        if res=="OK":
            #set integration time to the double of the minimum and then the minimum
            #to check whether integration time change works
            for it in [self.min_it_ms * 2,self.min_it_ms]:
                res=self.set_it(it)
                if res!="OK":
                    break
            sleep(0.2)

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

        return res

    def set_it(self,it_ms):
        """
        Set spectrometer integration time (=Exposure time)

        params:
            <it_ms>: Integration time in milliseconds (float)
        """
        res="OK"
        ss="Setting integration time to "+str(it_ms)+" ms"
        if self.simulation_mode:
            if self.debug_mode>=2:
                self.logger.debug(ss+" (Simulation mode)")
            self.it_ms=it_ms
        else:
            if self.debug_mode>=2:
                self.logger.debug(ss)
            res,_=self.set_value(DCAM_IDPROP.EXPOSURETIME, float(it_ms) * 1.0e-3, err_mode="warn") #set in seconds
            if res=="OK":
                self.it_ms=it_ms
            else:
                res = "Could not set integration time to "+str(it_ms)+" ms. Error: "+res
        self.error=res
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

        Finally, once all cycles are measured, read, and handled, the data_handling_watchdog thread will calculate
        the mean and standard deviation, and will set the flag self.measuring to False.

        Then, the internal_meas_done_event is set, to indicate to other functions of this library that the measurement
        is complete. If the external_meas_done_event is not None, it will be set as well to notify the possible parent
        modules out of this library that the measurement is complete. (This is the one used by BlickO spec_controller)

        This can be much simpler, but in this way we ensure the spec is already measuring the next cycle while
        we are handling the data of the previous cycle.
        """
        self.measuring=True
        self.internal_meas_done_event.clear()
        self.docatch=True
        self.error="OK" #Reset last error

        #Put ncy into the data_read_queue:
        self.read_data_queue.put(ncy)

        return self.error

    def abort(self,ignore_errors=False):
        """
        res=self.abort(ignore_errors=False)

        Send an order to the spectrometer in order to stop any ongoing measurement.

        params:
         <ignore_errors>: if True, ignore any error that may happen while stopping the measurement (boolean)

        return:
            <res>: string with the result of the operation. It can be "OK" or an error description.
        """
        self.docatch=False # -> Stop adding new data arrivals into the read data queue.
        res="OK"
        if self.simulation_mode:
            if self.debug_mode>1:
                self.logger.info("stop_measure, Stopping any ongoing measurement (simulation mode)...")
        else:
            if self.debug_mode>0:
                self.logger.info("stop_measure, Stopping any ongoing measurement...")
            if self.spec_handler is None:
                self.logger.info("stop_measure, skipping abort measure because there is no spec handler (previously disconnected).")
            else:
                try:
                    resdll=self.spec_handler.cap_stop()
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
        Read an auxiliary sensor of the spectrometer. (Temperature, Humidity, etc)
        """
        res="OK"
        if self.simulation_mode:
            value=11.1
            return res, value

        if sname == "detector":
            idprop = DCAM_IDPROP.SENSORTEMPERATURE
            res, value = self.get_value(idprop)
            if res == "OK":
                value = float(value)
                if self.debug_mode >= 2:
                    self.logger.debug("read_aux_sensor, " + sname + " temperature: " + str(value) + " degC")
            else:
                value = -99.0
                self.logger.warning("read_aux_sensor, could not read '" + sname + "' temperature, err= " + res)
        else:
            value = -99.0
            res="Unknown sensor name: '"+sname+"' for spec "+self.alias
            self.logger.error(res)

        return res,value

    def disconnect(self,dofree=False,ignore_errors=False):
        """
        Deactivate and disconnect spectrometer from dll.
        params:
            <dofree>: (boolean) Set this to True to finalize the dll communication as well (clear dll_handler).
            Note that this parameter is only having effect when there is only one spectrometer connected
            (ie when all the other spectrometers have been already disconnected). Otherwise the dll communication will not be closed.

            <ignore_errors>: (boolean) Set this to True to ignore any error that happens during the disconnection process.

        """
        res="OK"

        #Remove the instance from the Hama2_Spectrometer_Instances dictionary: So that no more data is read from this spectrometer.
        if self.spec_id in Hama2_Spectrometer_Instances:
            del Hama2_Spectrometer_Instances[self.spec_id]

        if self.simulation_mode:
            self.logger.info("Disconnecting spectrometer "+self.alias+"... (Simulation mode)")
        else:
            if self.spec_handler is not None:
                self.logger.info("Disconnecting spectrometer "+self.alias+"...")

                #Deactivate spec:
                # It closes the allocated buffer and then
                # closes communication with selected spectrometer (clear self.spec_id)
                _=self.deactivate(ignore_errors=ignore_errors)
                self.logger.info("Spectrometer "+self.alias+" disconnected.")

                #Finalize the dll communication (clear dll_handler):
                if dofree:
                    res=self.close_spec_dll(ignore_errors=ignore_errors)
            else:
                self.logger.info("Skipping disconnection: Spectrometer "+self.alias+" is already disconnected.")

        if self.data_arrival_watchdog_thread is not None:
            self.logger.info("Closing data arrival watchdog thread of spectrometer "+self.alias+".")
            self.read_data_queue.put((None)) #Send a "stop" signal to the data handling watchdog thread.
            self.data_arrival_watchdog_thread.join() #Wait for the data arrival watchdog thread to finish.
            self.data_arrival_watchdog_thread=None
        if self.data_handling_watchdog_thread is not None:
            self.logger.info("Closing data handling watchdog thread of spectrometer "+self.alias+".")
            self.handle_data_queue.put((None,(None,None,None))) #Send a "stop" signal to the data arrival watchdog thread.
            self.data_handling_watchdog_thread.join() #Wait for the data handling watchdog thread to finish.
            self.data_handling_watchdog_thread=None
            self.logger.info("Side watchdog threads of spectrometer "+self.alias+" closed.")
        self.error=res
        return res

    def recovery(self,ntry=3, dofree=False):
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

        """
        self.docatch=False # -> Stop adding new data arrivals into the read data queue.

        # The recovery procedure if this "DCAMERR.LOSTFRAME" error happens is a bit different from for other errors,
        # since this error can only be solved via spec power reset. Same for "DCAMERR.TIMEOUT".
        # The spec controller will call this "soft" recovery function. If this soft recovery fails, then
        # it will proceed to do a spec power reset.

        # Procedure for "DCAMERR.LOSTFRAME" or "DCAMERR.TIMEOUT" error:
        if self.last_errcode=="DCAMERR.LOSTFRAME" or self.last_errcode=="DCAMERR.TIMEOUT":
            res = str(self.last_errcode)+" error cannot be recovered via soft recovery -> soft recovery failed"
            self.logger.warning(res)
            self.last_errcode=0 #clear error code so that next try proceeds to the regular recovery procedure
            _ = self.disconnect(dofree=True) #finish the dll session here to ensure a new one can be created after power reset.
            return res

        # Regular recovery procedure for other errors, or for after power reset:
        for i in range(ntry):

            self.logger.warning("Recovering spectrometer "+self.alias+"... (try "+str(i+1)+"/"+str(ntry)+")")

            #Try to stop any ongoing measurement:
            if self.spec_handler is None:
                res="NOK" #Skip this section, execute the next one.
            else:
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
                    self.logger.info("Spectrometer "+self.alias+" could set the last integration time ("+str(self.it_ms)+" ms).")

                    #try to read an aux sensor
                    res,_=self.read_aux_sensor(sname="detector")
                    if res=="OK":
                        self.logger.info("Spectrometer "+self.alias+" could read an auxiliary sensor. ")

                if res=="OK":
                    #Check if the system is alive
                    _, value = self.get_value(DCAM_IDPROP.SYSTEM_ALIVE, err_mode="ignore") # I suspect it always returns 2
                    alive_status={1:"OFFLINE", 2:"ONLINE", 3:"ERROR"}
                    status = alive_status.get(value, "UNKNOWN")
                    self.logger.info("System alive status: "+str(status))
                    if status=="ONLINE":
                        self.logger.info("Spectrometer "+self.alias+" system alive status is ONLINE. -> Soft recovery finished successfully.")
                        break #Quit ntry for loop and exit


            if res!="OK":
                #If the code reach this point, the most possible reason is that the spectrometer lost its power while it was measuring
                #and the old dll handler is not valid anymore. It is needed to re-initialize the dll and re-connect the spec.

                #Try to disconnect the spectrometer, and finish the data arrival and data handling watchdog threads:
                _=self.disconnect(dofree=True) #Note: for hama2 we forcedly have to use dofree, otherwise soft recovery
                # won't work, and the software will keep blocked at quitting
                sleep(2) #wait a bit

                #Try to connect the spectrometer again:
                res=self.connect()
                if res=="OK":
                    #Set the last integration time set:
                    res=self.set_it(it_ms=self.it_ms)

                if res=="OK":
                    self.logger.info("Spectrometer "+self.alias+" could be reconnected and last integration time set -> Soft recovery finished successfully.")
                    break # quit for loop
                else:
                    self.logger.warning("Recovery of spectrometer "+self.alias+" failed.")
                    sleep(3)

        return res

    #---Main Control Functions (used by this library)---

    def measure_blocking(self,ncy=10):
        """
        Send an order to the spectrometer, in order to get ncy measurements.
        It is a blocking call until the spectrometer finishes the entire measurement

        This function measures all cycles one by one, without using threads.
        """
        self.measuring=True


        #Abort any ongoing measurement
        _ = self.abort(ignore_errors=True)
        self.internal_meas_done_event.clear()

        self.ncy_requested=ncy
        self.reset_spec_data() #reset accumulated data and measurements done/handled counters

        # Check camera is ready:
        res, status = self.get_status()
        if res!="OK" or status!="READY":
            # res="Could not start measurement. Error: "+res
            # self.logger.error(res)
            # self.error=res
            # return res
            self.logger.warning("Camera reported wrong status: "+str(status))
            res="OK" #Temporal patch

        # Calculate max cycles per measurement call:
        # Vary the number of cycles per measurement pack depending on the integration time:
        if self.it_ms < self.max_it_ms_for_meas_pack:
            self.max_ncy_per_meas = self.max_ncy_per_meas_default
        else:
            self.max_ncy_per_meas = 1

        # Split requested ncy in chunks:
        self.ncy_per_meas, packs_info = split_cycles(self.max_ncy_per_meas, ncy)

        # Start ncycles measurement loop
        if self.debug_mode>0:
            self.logger.info("Starting measurement, ncy="+str(ncy)+", IT="+str(self.it_ms)+" ms, npacks="+packs_info)

        self.meas_start_time=spec_clock.now() #Set measurement start time

        self.docatch = True
        for pack_idx, ncy_pack in enumerate(self.ncy_per_meas):

            if not self.docatch:
                res = "Measurement Aborted"
                self.logger.info(res)
                break

            if self.debug_mode > 2:
                self.logger.debug("Measuring pack " + str(pack_idx + 1) + "/" + str(len(self.ncy_per_meas)) + \
                                  ", ncy_pack=" + str(ncy_pack) + ", cycles from " + str(self.ncy_read) + " to " + \
                                  str(self.ncy_read + ncy_pack))
            res = self.measure_pack(ncy_pack)
            if res!="OK":
                res = "Could not measure pack "+str(pack_idx+1)+"/"+str(len(self.ncy_per_meas))+ \
                      ", ncy_pack="+str(ncy_pack)+", IT="+str(self.it_ms)+"ms. "+res
                self.logger.warning(res)
                break
            elif self.internal_meas_done_event.is_set():
                #Measurement was aborted due to saturation detected in measure_pack(), or measurement finished
                break

        # If all cycles were read without problems, res=="OK".
        # Otherwise, res contains the error description.

        # Final cleanup
        self.measuring = False
        self.error = res

        # Inform (any) parent module that the measurement is complete
        if self.external_meas_done_event is not None and not self.recovering:
            self.external_meas_done_event.set()

        return res

    def measure_pack(self, ncy_pack=8):
        """
        Send an order to the spectrometer, in order to get a pack of ncy_pack measurements.
        It is a blocking call until the spectrometer finishes the entire measurement of the pack.

        Params:
         - ncy_pack (int): number of cycles to be measured in this pack

         Note that hama2 spectrometers can accept a maximum of 1024 cycles per measurement pack
         (limited by the maximum allowed value of LINEBUNDLEHEIGHT.
        """
        if self.simulation_mode:
            simulated_duration = ncy_pack * (self.it_ms / 1000.0) * self.simudur  # Convert ms to seconds
            # Simulated duration will be reduced by simudur factor, for faster simulations
            sleep(simulated_duration)
            # Create ramdom data:
            rc = np.random.randint(int(self.simulated_rc_min / self.discriminator_factor),
                                   int(self.simulated_rc_max / self.discriminator_factor),
                                   (ncy_pack,self.npix_active)) #2D array with ncy_pack rows
            self.arrival_times.append(spec_clock.now()) #In this case there will be only 1 arrival time = time when pack of cycles arrived.
            for i in range(ncy_pack):
                self.ncy_read += 1
                issat = self.handle_cycle_data(self.ncy_read, rc[i,:], [], [])
                if issat and self.abort_on_saturation:
                    self.logger.info(
                        "Measurement aborted due to saturation in cycle " + str(self.ncy_read) + "/" + str(self.ncy_requested))
                    # Finish measurement
                    self.measurement_done()
                    break  # Stop measuring more cycles -> quit for loop
                else:
                    # Continue even if saturation is detected:
                    if self.ncy_handled == self.ncy_requested:
                        self.measurement_done()
                        break  # All done -> quit for loop
            return "OK"

        # Online mode - non simulation mode:

        # Set line bundle height to get ncy_pack spectra in one measurement call:
        if self.line_bundle_height != ncy_pack:
            res = self.set_line_bundle_height(ncy_pack,log=False) #1 or 8
            if res != "OK":
                return res

        # Request measurement (1 cycle that will contain ncy_pack spectra)
        resdll = self.spec_handler.cap_snapshot()
        res = self.get_error(resdll)
        if res != "OK":
            res = "Could not start measurement. Error: " + res
            self.logger.error(res)
            return res

        # Wait for measurement done
        cdt = 1.0 # estimated cycle delay time
        cy_timeout_ms = ncy_pack * (self.it_ms + cdt) + self.cycle_timeout_ms
        resdll = self.spec_handler.wait_capevent_frameready(int(cy_timeout_ms))  # Blocking call until measurement is done or error
        arrival_time=spec_clock.now()
        res = self.get_error(resdll)
        if res != "OK":
            res = "Error while waiting for data: "+res
            self.logger.error(res)
            # Query system alive status:
            # _, value = self.get_value(DCAM_IDPROP.SYSTEM_ALIVE, err_mode="ignore")
            # self.logger.info("System alive status: "+str(value)) -> always returns 2
            # Abort operation in the spectrometer, reset timeout timers.
            # _=self.spec_handler.cap_stop()
            return res

        # Get measurement data
        rc = self.spec_handler.buf_getlastframedata()
        if isinstance(rc, bool):
            res = "Could not get data from buffer. Error: " + self.get_error(rc)
            self.logger.error(res)
            return res

        # Check size of rc:
        if rc.shape[0] != ncy_pack or rc.shape[1] != self.npix_active:
            res = "Wrong size of buffer data. Expected ("+str(ncy_pack)+"," +str(self.npix_active)+"), got "+str(rc.shape)
            self.logger.error(res)
            return res

        # Get spectra from received data
        self.arrival_times.append(arrival_time) #In this case there will be only 1 arrival time = time when pack of cycles arrived.
        for i in range(ncy_pack):
            rc_i = rc[i, :]  # Get spectra as 1D array, from 2D array with ncy_pack rows
            if self.debug_mode > 2:
                self.logger.debug("Data arrived for cycle " + str(i + 1) + "/" + str(ncy_pack))
            self.ncy_read += 1
            issat = self.handle_cycle_data(self.ncy_read, rc_i, [], [])
            if issat and self.abort_on_saturation:
                self.logger.info(
                    "Measurement aborted due to saturation in cycle " + str(self.ncy_read) + "/" + str(self.ncy_requested))
                # Abort operation in the spectrometer, reset timeout timers.
                _ = self.spec_handler.cap_stop()
                # Finish measurement
                self.measurement_done()
                break  # Stop handling more cycles from the already measured ones.
            else:  # Continue even if saturation is detected:
                if self.ncy_handled == self.ncy_requested:
                    self.measurement_done()
                    break  # quit for loop
                else:
                    continue  # proceed to handle the next cycle
        return "OK"

    def measure_blocking_threads(self,ncy=10):
        """
        Send an order to the spectrometer, in order to get ncy measurements.
        It is a blocking call until the spectrometer finishes the entire measurement

        This function measures and read all cycles one by one, and uses threads to handle the read data separately.
        """
        self.measuring=True

        #Abort any ongoing measurement
        _ = self.abort(ignore_errors=True)
        self.internal_meas_done_event.clear()
        self.docatch=True

        self.ncy_requested=ncy
        self.reset_spec_data() #reset accumulated data and measurements done/handled counters

        cy_timeout_ms = self.it_ms + self.cycle_timeout_ms

        # Check camera is ready:
        res, status = self.get_status()
        if res!="OK" or status!="READY":
            # res="Could not start measurement. Error: "+res
            # self.logger.error(res)
            # self.error=res
            # return res
            self.logger.warning("Camera reported wrong status: "+str(status))
            res="OK" #Temporal patch

        # Start ncycles measurement loop
        if self.debug_mode>0:
            self.logger.info("Starting measurement, ncy="+str(ncy)+", IT="+str(self.it_ms)+" ms")

        self.meas_start_time=spec_clock.now() #Set measurement start time
        for i in range(ncy):

            if self.simulation_mode:
                simulated_duration = self.simudur * (self.it_ms / 1000.0)  # Convert ms to seconds
                # Simulated duration will be reduced by simudur factor, for faster simulations
                sleep(simulated_duration)
                #Create ramdom data:
                rc=np.random.randint(int(self.simulated_rc_min/self.discriminator_factor),
                                     int(self.simulated_rc_max/self.discriminator_factor),
                                     self.npix_active)
                self.arrival_times.append(spec_clock.now())
                self.ncy_read+=1
                self.handle_data_queue.put((deepcopy(self.ncy_read), (deepcopy(rc),[],[]) ))
                continue # proceed to next cycle

            resdll=self.spec_handler.cap_snapshot()
            res=self.get_error(resdll)
            if res!="OK":
                res="Could not start measurement. Error: "+res
                self.logger.error(res)
                break

            # Wait for data
            resdll=self.spec_handler.wait_capevent_frameready(int(cy_timeout_ms)) #Blocking call
            res=self.get_error(resdll)
            if res!="OK":
                res="Error while waiting for data. Error: "+res
                self.logger.error(res)
                #Query system alive status:
                _, value = self.get_value(DCAM_IDPROP.SYSTEM_ALIVE, err_mode="ignore")
                self.logger.info("System alive status: "+str(value))
                #Abort
                _=self.spec_handler.cap_stop()
                break # do not measure more cycles

            # Get data
            rc=self.spec_handler.buf_getlastframedata()
            if isinstance(rc,bool):
                res="Could not get data. Error: "+self.get_error(rc)
                self.logger.error(res)
                break # do not measure more cycles
            elif not self.docatch:
                self.logger.info("Measurement was aborted...") #i.e: when saturation was detected in the previous cycle
                res="OK"
                break # do not measure more cycles
            else:
                rc=rc[0,:] #convert 2D array with only one row to 1D array
                self.arrival_times.append(spec_clock.now())
                self.ncy_read+=1
                if self.debug_mode>2:
                    self.logger.debug("Data arrived for cycle "+str(i+1)+"/"+str(ncy))
                #Enqueue data for handling
                self.handle_data_queue.put((deepcopy(self.ncy_read),(deepcopy(rc),[],[]))) #(ncy_read,(rc,rc_blind_left,rc_blind_right))

        # Wait until all data has been handled
        if res=="OK":
            res = self.wait_for_measurement()
        else:
            #Empty the handle_data_queue (discard data)
            while not self.handle_data_queue.empty():
                _=self.handle_data_queue.get()
            self.logger.error(res)

        self.error=res
        return res


    def wait_for_measurement(self):
        """
        res=wait_for_measurement()
        Wait for the measurement to be complete
        Blocking call function that will wait until all spectrometer read data is handled.
        returns:
            <res>: If any problem happened while measuring, the last produced error will be stored in self.error
        """
        self.internal_meas_done_event.wait()
        return self.error

    #---Auxiliary functions (used by the Main functions)---

    def get_error(self,resdll):
        """
        res=get_error(resdll)
        Get the error description of an unsuccessful dll return answer.

        params:
            <resdll>: returned answer from a dll call (integer)

        return:
            <res>: error description (string)
        """
        #Check dll answer meaning
        if isinstance(resdll, bool) and not resdll:
            #Wrong answer, get error
            try:
                if self.spec_handler is None:
                    #Get last error from the dll handler
                    res = str(self.dll_handler.lasterr())
                else:
                    #Get last error from the specific spec dll handler
                    res = str(self.spec_handler.lasterr())
                #Save last generated error code, converted to string: (would help in the recovery procedure)
                self.last_errcode = res
            except Exception as e:
                res = "Exception while reading the last error for resdll = "+str(resdll)+": "+str(e)
        else:
            res="OK"
        return res

    def set_value(self, idprop, value, err_mode="check"):
        """
        res, value_set =self.set_value(idprop, value)
        Set the value of a property of the spectrometer.
        params:
            <idprop>: property identifier (a DCAM_IDPROP parameter)
            <value>: value to set (float)
            <err_mode>: (str) Controls the behavior of the function for when the value to be set was not properly set.
             if err_mode = "ignore" -> (default), the function will NOT return an error message if value_set != value, and will NOT warn about that.
             if err_mode = "warn" -> Do not return an error but do WARN in the logger that the value_set != value.
             if err_mode = "check" -> return an error message if value_set != value.
        return:
            <res>: string with the result of the operation. It can be "OK" or an error description.
            <value_set>: the value that was really set (float)
        """
        value_set = None
        try:
            if self.debug_mode>=2:
                self.logger.info("Setting value of property "+str(idprop.name)+" to "+str(value)+".")
            resdll=self.spec_handler.prop_setvalue(idprop.value, value) #Returns True or False
            res=self.get_error(resdll)
            if res!="OK":
                res="Could not set value of property "+ str(idprop.name)+" to " + str(value) +". Error: "+res
                self.logger.error(res)
            else:
                #Check the value that was really set:
                resdll=self.spec_handler.prop_getvalue(idprop.value)
                if isinstance(resdll,bool) and resdll==False:
                    res="Could not get value of property "+str(idprop.name)+" to check if it was set correctly."
                    self.logger.error(res)
                else:
                    value_set = resdll
                    if (value_set - value)>1e-6: # There could be rounding issues
                        res="Could not set value of property "+str(idprop.name)+" to "+str(value)+ \
                             ". Current value is "+str(value_set)
                        if err_mode=="warn":
                            self.logger.warning(res)
                            res="OK"
                        else:
                            self.logger.error(res)
        except Exception as e:
            res="Exception happened while setting value of property "+str(idprop.name)+" to "+str(value)+": "+str(e)
            self.logger.exception(e)
        self.error=res
        return res, value_set

    def get_value(self, idprop, err_mode="check"):
        """
        res, value = self.get_value(idprop)
        Get the value of a property of the spectrometer.

        params:
            <idprop>: property identifier (DCAM_IDPROP object)
            <err_mode>: (str) Controls the behavior of the function for when the value to be set was not properly set.
             if err_mode = "ignore" -> the function will NOT return an error message if value cannot be read
             if err_mode = "warn" -> Do not return an error but do WARN in the logger that the value could not be read.
             if err_mode = "check" -> return an error message if the value cannot be read
        """
        res, value = "OK", None
        try:
            resdll = self.spec_handler.prop_getvalue(idprop.value)
            if isinstance(resdll, bool) and not resdll:
                if err_mode == "check":
                    res = "Could not get value of property "+str(idprop.name)+". Error: "+self.get_error(resdll)
                elif err_mode=="warn":
                    self.logger.warning(res)
                    res="OK"
                elif err_mode=="ignore":
                    res="OK"
            else:
                value = resdll
        except Exception as e:
            res = "Exception happened while getting value of property "+str(idprop.name)+": "+str(e)
            self.logger.exception(e)
        self.error = res
        return res, value

    def get_status(self):
        """
        res,status=self.get_status()
        Get the status of the spectrometer.
        return:
            <res>: string with the result of the operation. It can be "OK" or an error description.
            <status>: status of the spectrometer (string)
        see dcamapi4.DCAMCAP_STATUS for possible status values.
        """
        res="OK"
        status=None
        if self.simulation_mode:
            if self.debug_mode>1:
                self.logger.info("get_status, simulating a connected spectrometer device.")
            status="READY"
        else:
            try:
                resdll=self.spec_handler.cap_status()
                res=self.get_error(resdll)
                if res=="OK":
                    status=DCAMCAP_STATUS(resdll).name
                else:
                    res="Could not get status of the spectrometer, error: "+res
            except Exception as e:
                res="Exception happened while getting the status of the spectrometer: "+str(e)
                self.logger.exception(e)
        self.error=res
        return res, status

    #Auxiliary functions related to connection:

    def load_and_init_hama2_dll(self):
        """
        Load the spectrometer control dll

        returns
        <res>: string with the result of the operation. It can be "OK" or an error description.
        """
        res="OK"
        self.logger.info("Loading spectrometer dll...")
        global spec_dll_initialized

        if not spec_dll_initialized:
            resdll=self.dll_handler.init()
            res=self.get_error(resdll)
            if res=="OK":
                self.logger.info(self.spec_type+" dll initialized successfully.")
                spec_dll_initialized=True
            else:
                if "-2147483130" in res or "NOCAMERA" in res.upper():
                    res=("No Hamamatsu camera detected (DCAMERR.NOCAMERA). "
                         "Check USB cable, camera power, and that the Hamamatsu "
                         "DCAM-API USB driver is installed (Device Manager should "
                         "list the camera, not an 'Unknown device').")
                else:
                    res="Could not initialize "+self.spec_type+" dll: "+res
                self.logger.error(res)
        else:
            self.logger.info(self.spec_type+" dll already initialized.")
        return res

    def enable_dll_logging(self,enable):
        """
        Enables or disables writing dll debug information to a log file

        params:
         <enable>: Boolean, True enables logging, False disables logging
        return:
         <res>: "OK" or an error description
        """
        res="OK"
        if self.simulation_mode:
            pass
        else:
            raise NotImplementedError("enable_dll_logging is not implemented for this spectrometer model.")

        self.error=res
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
            self.logger.info("get_number_of_devices, simulating a connected spectrometer device.")
            ndev=1
        else:
            try:
                ndev=self.dll_handler.get_devicecount()
                if isinstance(ndev,bool) and ndev==False:
                    res="Could not get number of connected "+self.spec_type+" spectrometers. Dll not initialized"
                else:
                    if ndev<1:
                        res="No "+self.spec_type+" spectrometers found connected to the USB port."
                    else:
                        self.logger.info("Number of connected "+self.spec_type+" spectrometers: "+str(ndev))
            except Exception as e:
                res="Exception happened while getting the number of spectrometers: "+str(e)
                self.logger.exception(e)

        self.error=res #Update last error.

        return res,ndev

    def get_all_devices_info(self,ndev):
        """
        res,devs_info=self.get_all_devices_info(ndev)

        Returns the device information of all connected spectrometers.

        params:
            <ndev>: Number of connected devices (integer)

        return:
            <res>: string with the result of the operation. It can be "OK" or an error description.
            <devs_info>: device information of all connected spectrometers
        """

        res="OK"
        self.logger.info("Getting device information of all connected spectrometers...")

        devs_info={}

        for i in range(ndev):
            devs_info[i]={}
            dcam=Dcam(i)
            #Get model
            model=dcam.dev_getstring(DCAM_IDSTR.MODEL) #Returns str or bool
            if model:
                devs_info[i]["model"]=str(model)
            else:
                devs_info[i]["model"]=None
            #Get device id
            cameraid=dcam.dev_getstring(DCAM_IDSTR.CAMERAID)
            if cameraid:
                devs_info[i]["id"]=str(cameraid)
            else:
                devs_info[i]["id"]=None

            self.logger.info(devs_info)

        return res, devs_info

    def find_spec_info(self, devs_info):
        """
        Find the id of the spectrometer with the correct serial number.
        :return:
        """
        spec_id=None
        spec_info={}
        res="OK"
        for dev_num in devs_info.keys():
            dev_sn=str(devs_info[dev_num].get("id","")).strip("\x00").strip()
            if dev_sn==self.sn:
                spec_id = dev_num
                spec_info = devs_info[dev_num]
                self.logger.info("Found "+self.spec_type+" spectrometer with serial number "+self.sn+".")
                break
        else:
            sns_found=[str(devs_info[k].get("id","")) for k in devs_info]
            res="No "+self.spec_type+" spectrometer found with serial number "+self.sn+". Connected: "+str(sns_found)
        return res, spec_id, spec_info

    def get_spec_handler(self,dev_num):
        """
        res, spec_handler = self.get_spec_handler(device_number)

        Activates a Hama2 spectrometer, and gets a device handler to work with.

        params:
            <dev_num>: number of the spectrometer to activate, based on the list of spectrometers queried by
            get_all_devices_info() (integer)

        return:
            <res>: string with the result of the operation. It can be "OK" or an error description.
            <dll_handler>: The specific spectrometer dll handler.
        """
        res="OK"
        spec_handler=None
        self.logger.info("Getting spectrometer handler ...")
        if self.simulation_mode:
            pass
        else:
            try:
                spec_handler = Dcam(dev_num)
                resdll=spec_handler.dev_open()
                res=self.get_error(resdll)
                if res!="OK":
                    res="Could not get device handler. Error: "+res
            except Exception as e:
                res="Exception happened while opening communication with spectrometer: "+str(e)
                self.logger.exception(e)

        return res, spec_handler

    def get_device_config(self):
        """
        res=self.get_device_config()
        Get the device config parameters of the spectrometer.
        (Testing function, just to confirm if some parameters are being set correctly)
        """
        self.logger.info("Getting device config parameters of spec "+self.alias+"...")
        res="OK"

        idprop = self.spec_handler.prop_getnextid(0)
        while idprop is not False:
            output=str(idprop)+": "
            propname = self.spec_handler.prop_getname(idprop)
            output+=propname
            propattr = self.spec_handler.prop_getattr(idprop)
            output+=", min="+str(propattr.valuemin)+", max="+str(propattr.valuemax)+", step="+str(propattr.valuestep)+", def="+str(propattr.valuedefault)
            currentvalue=self.spec_handler.prop_getvalue(idprop)
            output+=", curr="+str(currentvalue)
            #Check meaning of the value, if any
            try:
                idprop_object = DCAM_IDPROP(idprop)
                if hasattr(DCAMPROP, idprop_object.name):
                    prop_class = getattr(DCAMPROP,idprop_object.name)
                    if currentvalue in [item.value for item in prop_class]:
                        output+=", (="+str(prop_class(currentvalue).name)+")"
                        output+=", dll recognized values: "+str(prop_class.__members__.items())
            except:
                pass

            self.logger.info(output)
            idprop = self.spec_handler.prop_getnextid(idprop)

        return res

    def set_device_config(self):
        """
        res=self.set_device_config(devconfig)
        Set the device config parameters of the spectrometer.
        These are the parameters that needs to be (re)set after a power reset.
        """
        res="OK"

        # Set internal clock to its fasttest allowed speed
        propattr=self.spec_handler.prop_getattr(DCAM_IDPROP.READOUT_FREQUENCY)
        max_freq=propattr.valuemax
        res,_=self.set_value(DCAM_IDPROP.READOUT_FREQUENCY, max_freq, err_mode="check")
        if res!="OK":
            return res
        # Check current clock speed:
        res, value = self.get_value(DCAM_IDPROP.READOUT_FREQUENCY)
        if res!="OK":
            return res
        self.logger.info("READOUT_FREQUENCY was successfully set to "+str(value)+" MHz")


        # Set spectrometer cooler setting temperature
        if self.spec_cooler_set_temp is not None:
            res=self.set_cooler_setting_temp(self.spec_cooler_set_temp, err_mode="check")
            if res!="OK":
                return res

        #Set contrast gain (only if self.contrast_gain!=-1):
        if self.contrast_gain is not None:
            res=self.set_contrast_gain(self.contrast_gain, err_mode="check")
            if res!="OK":
                return res

        #Set line bundle height to 1
        res = self.set_line_bundle_height(1, err_mode="check", log=True)
        if res!="OK":
            return res

        return res

    def reset_device_config(self):
        """
        res=self.reset_device_config()
        Resets onboard device parameter section to its factory defaults. This command will result in
        the loss of all user-specific device configuration settings
        """
        raise NotImplementedError("reset_device_config is not implemented for this spectrometer model.")

    def get_number_of_pixels(self):
        """
        res=self.get_number_of_pixels()
        Get the number of pixels of the device specified by self.spec_id.
        """
        npix=0
        res,value = self.get_value(DCAM_IDPROP.SUBARRAYHSIZE)
        if res != "OK":
            res="Could not get the number of pixels. Error: "+res
        else:
            npix=int(value)
            self.logger.info("Number of pixels reported by dll:"+str(npix))
        self.error=res
        return res, npix

    def get_cooler_settings(self):
        """Read the current spec cooler settings"""
        self.logger.info("Getting spectrometer cooler settings...")
        res,value=self.get_value(DCAM_IDPROP.SENSORCOOLER)
        if res=="OK":
            status=DCAMPROP.SENSORCOOLER(value).name
            self.logger.info("SENSORCOOLER: "+str(value)+" (="+status+")")

        res,value=self.get_value(DCAM_IDPROP.SENSORTEMPERATURETARGET)
        if res=="OK":
            self.logger.info("SENSORTEMPERATURETARGET: "+str(value)+" (= set temp degC)")

        res,value=self.get_value(DCAM_IDPROP.SENSORTEMPERATURE)
        if res=="OK":
            self.logger.info("SENSORTEMPERATURE: "+str(value)+" (= current temp degC)")

        res,value=self.get_value(DCAM_IDPROP.SENSORCOOLERSTATUS)
        if res=="OK":
            status=DCAMPROP.SENSORCOOLERSTATUS(value).name
            self.logger.info("SENSORCOOLERSTATUS: "+str(value)+" (="+status+")")

        res,value=self.get_value(DCAM_IDPROP.SENSORCOOLERFAN)
        if res=="OK":
            self.logger.info("SENSORCOOLERFAN: "+str(value))
        else:
            self.logger.info("SENSORCOOLERFAN: Do not exist")

        return "OK"

    def set_cooler_setting_temp(self, value=0, err_mode="check"):
        """
        res=self.set_cooler_setting_temp(value)
        Set the cooler setting temperature
        """
        #Turn on spec cooler
        self.logger.info("Turning spec cooler ON")
        res,_=self.set_value(DCAM_IDPROP.SENSORCOOLER,2, err_mode=err_mode) #1=off, 2=on
        if res!="OK":
            self.error=res
            return res

        #Set target temperature
        if res=="OK":
            self.logger.info("Setting  spec cooler target temperature to "+str(value))
            res,_=self.set_value(DCAM_IDPROP.SENSORTEMPERATURETARGET, float(int(value)), err_mode=err_mode)

        self.error=res
        return res

    def set_contrast_gain(self,value=0, err_mode="check"):
        """
        res=self.set_contrast_gain(value, err_mode="check")
        Selects the contrast gain
        possible values btw 0 and 3
        """
        self.logger.info("Setting contrast gain to "+str(value))
        res,_=self.set_value(DCAM_IDPROP.CONTRASTGAIN, value, err_mode=err_mode)
        self.error=res
        return res

    def set_line_bundle_height(self,value=1, err_mode="check", log=True):
        """
        res=self.set_line_bundle_height(value)
        Sets the line bundle height
        possible values btw 1 and 8
        """

        #release buffer
        resdll = self.spec_handler.buf_release()
        if not resdll:
            self.logger.warning("Could not release buffer ")
        else:
            if self.debug_mode > 2:
                self.logger.debug("Spectrometer buffer cleared.")

        if log or self.debug_mode > 2:
            self.logger.info("Setting line bundle height to " + str(value))

        res, _ = self.set_value(DCAM_IDPROP.SENSORMODE_LINEBUNDLEHEIGHT, value, err_mode=err_mode)
        self.error = res
        if res != "OK":
            res = "Could not set line bundle height to " + str(value) + ". Error: " + res
            self.logger.error(res)
            return res

        # re-allocate buffer
        resdll = self.spec_handler.buf_alloc(1)
        res = self.get_error(resdll)
        if res != "OK":
            res = "Could not allocate buffer. Error: " + res

        #Update current line bundle height
        self.line_bundle_height = value

        return res



    def deactivate(self,ignore_errors=False):
        """
        Closes the dll communication of a previously activated spectrometer.
        It will remove the self.spec_id handler.
        """
        res="OK"

        if self.spec_handler is None:
            res="Could not deactivate spectrometer. spec_handler is not initialized."
            self.logger.error("deactivate, "+res)
            return res

        self.logger.info("Deactivating spectrometer...")
        if self.simulation_mode:
            pass
        else:
            try:
                resdll=self.spec_handler.buf_release()
                if not resdll:
                    self.logger.warning("Could not release buffer ")
                else:
                    self.logger.info("Spectrometer buffer cleared.")

                resdll=self.spec_handler.dev_close()
                if not resdll:
                    res="Could not close spec connection."
                    self.logger.warning(res)
                else:
                    self.logger.info("Spectrometer connection closed.")
                    self.spec_handler=None
            except Exception as e:
                res="Exception happened while deactivating spectrometer "+self.alias+": "+str(e)
                if ignore_errors:
                    self.logger.warning("deactivate, "+res)
                    res="OK"
                else:
                    self.logger.exception(e)

        self.spec_id=None #Release spec_id handler

        return res

    def close_spec_dll(self,ignore_errors=False):
        """
        Finalize the dll communication and release its internal storage.
        Note: call to this function only once at exit time, once all spectrometers have been disconnected!.
        Otherwise, all connected spectrometers will be unmanageable after calling this function.

        params:
            <ignore_errors>: (boolean) Set this to True to ignore any error that happens during the closing process.
        """
        res="OK"
        global spec_dll_initialized
        if not spec_dll_initialized:
            self.logger.info("Hama2 dll communication is already closed.")
            return res

        if len(Hama2_Spectrometer_Instances)!=0:
            #Instances will be deleted at disconnection time.
            self.logger.info("Not closing the dll communication because there is another spectrometer that is still needing it.")
        else: #This part will be executed only when all specs have been already disconnected.
            try:
                self.logger.info("Closing dll communication.")
                resdll=self.dll_handler.uninit()
                if not resdll:
                    res="Could not close dll."
                    self.logger.error("close_spec_dll, "+res)
                else:
                    self.logger.info("Hama2 dll communication closed.")
                    spec_dll_initialized=False
            except Exception as e:
                res="Exception happened while closing the dll: "+str(e)
                if ignore_errors:
                    self.logger.warning("close_spec_dll, "+res)
                    res="OK"
                else:
                    self.logger.exception(e)
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

        self.ncy_read=0 #Current number of cycles measured and read from the from the spectrometer roe
        self.ncy_handled=0 #Current number of cycles handled
        self.ncy_saturated=0 #Number of saturated measurements. (only active pixels checked)
        self.sy=np.zeros(self.npix_active,dtype=np.float64) #Sum of the counts
        self.syy=np.zeros(self.npix_active,dtype=np.float64) #Sum of the squared counts
        self.sxy=np.zeros(self.npix_active,dtype=np.float64) #Sum of the meas index by the counts
        self.sy_blind_left=np.zeros(self.npix_blind_left,dtype=np.float64) #Sum of the counts of the blind pixels at left side of the detector
        self.syy_blind_left=np.zeros(self.npix_blind_left,dtype=np.float64) #Sum of the squared counts of the blind pixels at left side of the detector
        self.sxy_blind_left=np.zeros(self.npix_blind_left,dtype=np.float64) #Sum of the meas index by the counts of the blind pixels at left side of the detector
        self.sy_blind_right=np.zeros(self.npix_blind_right,dtype=np.float64) #Sum of the counts of the blind pixels at right side of the detector
        self.syy_blind_right=np.zeros(self.npix_blind_right,dtype=np.float64) #Sum of the squared counts of the blind pixels at right side of the detector
        self.sxy_blind_right=np.zeros(self.npix_blind_right,dtype=np.float64) #Sum of the meas index by the counts of the blind pixels at right side of the detector
        self.arrival_times=[] #List of arrival times of the measurements (Time in which the callback function was called)
        self.meas_start_time=0 #Unix time in seconds when the measurement started
        self.meas_end_time=0 #Unix time in seconds when the measurement ended (data arrival time of the last measured cycle)
        self.data_handling_end_time=0 #Unix time in seconds when the data handling ended (all cycles received + handled + final data handling)



    #Auxiliary functions for data retrieval (read and handle data from spectrometer)

    def data_arrival_watchdog(self):
        """
        This function will run permanently in a side thread, and is constantly waiting to have
        something (ncy) in the self.read_data_queue.

        As soon as it gets something, it will proceed to measure and get the data from the spectrometer,
        and put this data into the self.handle_data_queue.

        When disconnecting the spectrometer, the disconnect function will send a "None" to the self.read_data_queue,
        to signal that the data arrival watchdog thread must be finished.
        """

        #Ensure read_data_queue is empty before start using it.
        while not self.read_data_queue.empty():
            _=self.read_data_queue.get()

        #Start infinite data arrival monitoring loop:
        while True:
            ncy=self.read_data_queue.get()
            if ncy is None: #Exit flag -> disconnect function must be finished.
                self.logger.info("Exiting data arrival watchdog thread of spectrometer "+self.alias+"...")
                break
            else: #Normal data arrival -> measure and get the data from the spectrometer:
                res=self.measure_blocking(ncy)

                #update last error:
                self.error=res

                #If error, signal that the measurement is "complete" to unblock the main thread in case of a measure_blocking call:
                if res!="OK":
                    self.internal_meas_done_event.set()

    def data_handling_watchdog(self):
        """
        This function will run permanently in a side thread, and is constantly waiting to have a packet of
        spectrometer raw data added into the self.handle_data_queue.
        For every raw data "packet" added into this queue, this function will proceed to handle the data, and check
        if the measurement is complete.
        If the measurement is complete, it will signal the self.internal_meas_done_event, which will unblock the main thread, in
        case of a measure_blocking call.
        """

        #Ensure data_handling_queue is empty before start using it.
        while not self.handle_data_queue.empty():
            _=self.handle_data_queue.get()

        #Start the infinite data handling loop:
        while True:
            (ncy_read,(rc,rc_blind_left,rc_blind_right))=self.handle_data_queue.get()
            if ncy_read is None: #Exit flag -> data arrival watchdog thread must be finished.
                self.logger.info("Exiting data handling watchdog thread of spectrometer "+self.alias+"...")
                break
            elif not self.docatch:
                continue #Ignore any data arrival
            else: #Normal data arrival -> handle cycle data:
                # Check for saturation
                issat=self.handle_cycle_data(ncy_read,rc,rc_blind_left,rc_blind_right)
                if issat and self.abort_on_saturation:
                    self.logger.info("data_handling_watchdog, saturation detected in spec "+self.alias+
                                     ", for nmeas read ="+str(ncy_read)+"/"+str(self.ncy_requested)+
                                     ". Aborting due to saturation...")
                    self.docatch=False #Ignore the data arrival events from now on.

                    #Wait until the current elements in the read_data_queue are processed:
                    while not self.read_data_queue.empty():
                        sleep(0.1)

                    #At this point no more data is going to be read from the spectrometer, and
                    #the dll is free to be called to stop the measurement.

                    #Send a command to the spectrometer to stop measuring the subsequent cycles:
                    _=self.abort()

                    #emtpy the handle_data_queue -> no more data will be handled:
                    while not self.handle_data_queue.empty():
                        _,_=self.handle_data_queue.get()

                    #Finish the measurement:
                    self.measurement_done()
                else: #no saturation, or continue even if saturation was detected:
                    if self.debug_mode>=3:
                        self.logger.debug("data_handling_watchdog, ncy handled="+ \
                                          str(self.ncy_handled)+"/"+str(self.ncy_requested))
                    #If measurement is complete:
                    if self.ncy_handled==self.ncy_requested:
                        self.measurement_done()

    def handle_cycle_data(self,ncy_read,rc,rc_blind_left,rc_blind_right):
        """
        Handle the measurement cycle data
        This function will be called by the data handling watchdog, when a measurement has been already read from the
        spectrometer, and is ready to be handled.
        Data handling means convert the read raw counts from whatever format it comes (ctypes array) to a numpy array,
        and applying any needed post processing, to have the data in raw counts units.
        Then it is checked if the data is saturated, and if so, the saturated_meas_counter is incremented.
        Finally, the data is accumulated in the sy, syy, and sxy variables, that are used to calculate the mean and standard deviation
        at the end of the measurement.

        params:
            <ncy_read>: read measurement number (from 1 to requested_nmeas)
            <rc>: raw counts of the active pixels (np array)
            <rc_blind_left>: raw counts of the blind pixels on the left side of the detector (if any) (np array)
            <rc_blind_right>: raw counts of the blind pixels on the right side of the detector (if any) (np array)

        returns:
            <issat>: boolean, True if the last handled data is saturated, False otherwise.
        """
        cycle_index=ncy_read-1 #Index of the cycle in the accumulated data (0-based index)
        #Convert np.int16 to float64
        rc=rc.astype(np.float64)
        #Apply discriminator factor:
        if self.discriminator_factor!=1:
            rc=rc*float(self.discriminator_factor)
        rcmax=rc.max()
        rcmin=rc.min()
        #TODO: Check consistency of the data. (eg. all elements >0, no nans, etc.)
        if rcmin<0:
            self.logger.warning("handle_cycle_data, negative counts detected in spec "+self.alias+" data.")

        #Detect saturation:
        issat=rcmax>=self.eff_saturation_limit
        if issat and self.abort_on_saturation:
            #Do not add cycle data to accumulated data in this case.
            #No more cycles will be handled from now on.
            #This cycle data won't be used.
            #(in the accumulated data there won't be any saturated cycle)
            return issat #-> Quit

        else: #Continue even if saturation is detected:

            #Add cycle data to accumulated data for active pixels:
            self.sy=self.sy+rc
            self.syy=self.syy+rc**2
            self.sxy=self.sxy+cycle_index*rc

            #Do the same for blind pixels (if any):
            if len(rc_blind_left)>0:
                #Convert to float64
                rc_blind_left=rc_blind_left.astype(np.float64)
                #Apply discriminator factor:
                if self.discriminator_factor!=1:
                    rc_blind_left=rc_blind_left*float(self.discriminator_factor)
                #Add data to accumulated data:
                self.sy_blind_left=self.sy_blind_left+rc_blind_left
                self.syy_blind_left=self.syy_blind_left+rc_blind_left**2
                self.sxy_blind_left=self.sxy_blind_left+cycle_index*rc_blind_left

            if len(rc_blind_right)>0:
                #Convert to float64
                rc_blind_right=rc_blind_right.astype(np.float64)
                #Apply discriminator factor:
                if self.discriminator_factor!=1:
                    rc_blind_right=rc_blind_right*float(self.discriminator_factor)
                #Add data to accumulated data:
                self.sy_blind_right=self.sy_blind_right+rc_blind_right
                self.syy_blind_right=self.syy_blind_right+rc_blind_right**2
                self.sxy_blind_right=self.sxy_blind_right+cycle_index*rc_blind_right

            self.ncy_handled+=1
            if issat:
                self.ncy_saturated+=1

        return issat

    def measurement_done(self):
        """
        Final actions to be done when a measurement is complete.
        """
        self.meas_end_time=self.arrival_times[-1] #Unix Time in which the spectrometer indicated to the pc that
        # that the last cycle was finished and it was ready to be read.
        #Calculate mean, standard deviation and rms to a fitted straight line (for active pixels):
        x=np.arange(self.ncy_handled)
        _,self.rcm,self.rcs,self.rcl=calc_msl(self.alias,x,self.sxy,self.sy,self.syy)
        if self.npix_blind_left>0: #same for blind pixels
            _,self.rcm_blind_left,self.rcs_blind_left,self.rcl_blind_left=calc_msl(self.alias,x,self.sxy_blind_left,self.sy_blind_left,self.syy_blind_left)
            # when ncy == 1 : rcs and rcl are empty.
            # when ncy == 2 : rcs is not empty, rcl is empty.
            # when ncy > 2 : both rcs and rcl are not empty.
            if self.ncy_handled>2:
                self.rcl_blind_left=self.rcs_blind_left #replace rcl by rcs for blind pixels
        if self.npix_blind_right>0:
            _,self.rcm_blind_right,self.rcs_blind_right,self.rcl_blind_right=calc_msl(self.alias,x,self.sxy_blind_right,self.sy_blind_right,self.syy_blind_right)
            if self.ncy_handled>2:
                self.rcl_blind_right=self.rcs_blind_right #replace rcl by rcs for blind pixels

        if self.debug_mode>=1:
            self.logger.debug("Measurement done for spec "+self.alias)
        self.data_handling_end_time=spec_clock.now()
        self.docatch=False
        # Signal that the measurement is complete (internally)
        # This event will unblock any eventual measure_blocking function
        # when it is waiting to have all data handled
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
        -Determination of the mean and median cycle delay time
        -In case the cycles were measured one by one, the maximum and minimum deltas between the data arrival times.
        """
        real_dur_meas=1000.0*(self.meas_end_time-self.meas_start_time) #real duration of measurements [ms]
        real_dur_fdh=1000.0*(self.data_handling_end_time-self.meas_end_time) #real duration of the final data handling, [ms]
        real_dur_total=real_dur_meas+real_dur_fdh # real total duration (measurements + final data handling) [ms]
        expected_min_dur_meas=self.ncy_requested*self.it_ms #expected minimum duration of the measurements, [ms]. (=ncy*IT)
        #expected_min_dur_fdh=0 #expected minimum duration of final data handling, [ms]
        #expected_min_dur_tot=expected_min_dur_meas+expected_min_dur_fdh #expected minimum total duration, [ms]
        cdt_mean=max(0,(real_dur_meas-expected_min_dur_meas)/self.ncy_requested) #Mean cycle delay time of last measurement [ms/cy]
        deltas_max=np.nan
        deltas_min=np.nan

        #Calc the median cycle delay time (will be used to estimate the duration of a future measurement, as dur=ncy*(IT+cdt_median))
        #4 possible cases:
        #1) Measured ncy cycles one by one (self.max_ncy_per_meas==1), where only 1 cycle was measured (len(self.arrival_times)==1)
        #2) Measured ncy cycles one by one (self.max_ncy_per_meas==1), where more than 1 cycle was measured (len(self.arrival_times)>1)
        #3) Measured ncy cycles in X packs of Y cycles (self.max_ncy_per_meas>1), where only 1 pack was needed (len(self.arrival_times)==1) -> Not implemented!
        #4) Measured ncy cycles in X packs of Y cycles (self.max_ncy_per_meas>1), where more than 1 pack was needed (len(self.arrival_times)>1) -> Not implemented!

        if self.max_ncy_per_meas==1: #Measured ncy cycles one by one
            if len(self.arrival_times)==1:
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

            if len(self.arrival_times)==1: #Only one pack was needed.
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
        Note: Only valid for newest spectrometers, AS7007, the AS7010 and AS-MINI. (Not AS5216)
        """
        raise NotImplementedError("reset_device is not implemented for this spectrometer model.")

    def test_recovery(self):
        """
        Test the recovery function of the spectrometer after a communication failure.

        This function will order a measurement, then it will propose the user to unplug and plug the spectrometer USB from the PC,
        and then it will try to recover the spectrometer communication.
        """
        raise NotImplementedError("test_recovery is not implemented for this spectrometer model.")






if __name__ == "__main__":
    print("Testing Hama2 Spectrometer Class")

    #----Select Testing parameters----:
    # It is needed to install the DCAM_API software
    # Then the DCAM SDK loads the DCAM_API dll from C:/Windows/System32

    #Select spectrometer device to be tested:
    #instr="ACsc" #PaNIR Hama2 512pix

    instruments=["ACsc"]
    simulation_mode=[False]
    debug_mode=[3]
    #dll_logging=[True] #internal dll logging file
    do_recovery_test=False
    do_performance_test=False
    performance_test_fpath="C:/Temp"

    instruments=instruments[:]

    #---------Testing Code Start Here-----

    SP=[]
    for i in range(len(instruments)):
        instr=instruments[i]

        #Initialize an spectrometer instance:
        SP.append(Hama2_Spectrometer())

        #Configure instance:
        #SP[i].dll_path=dll_path
        SP[i].debug_mode=debug_mode[i]
        #SP[i].dll_logging=dll_logging[i]
        SP[i].simulation_mode=simulation_mode[i]
        SP[i].alias=str(i+1)

        if instr=="ACsc":
            SP[i].sn="125QC101"
            SP[i].npix_active=512
            SP[i].min_it_ms=0.008 #https://www.hamamatsu.com/content/dam/hamamatsu-photonics/sites/documents/99_SALES_LIBRARY/ssd/c16091_series_kacc1300e.pdf
            SP[i].contrast_gain=0 # Set to None if you don't want to set any contrast gain.
            SP[i].performance_test_it_ms_list=np.arange(0.006,10.1,1.0)
            SP[i].performance_test_ncy_list=[1, 10, 100, 200, 500, 1000] #Number of cycles to be tested for each IT
            SP[i].spec_cooler_set_temp=-20.0 # Set to None if you don't want to set any temp.
        else:
            raise("Instrument "+instr+" not recognized.")

        #Configure the logger for the spec alias:
        SP[i].initialize_spec_logger()

    #Configure logging:
    if np.any(debug_mode):
        loglevel=logging.DEBUG #TODO: Create independent loggers for each spectrometer
    else:
        loglevel=logging.INFO

    # logging formatter
    log_fmt="[%(asctime)s.%(msecs)03d] [%(levelname)s] [%(name)s] [%(message)s]"
    log_datefmt="%Y%m%dT%H%M%SZ"
    formatter = logging.Formatter(log_fmt, log_datefmt)

    # Configure the basic properties for logging
    logfile="C:/Temp/hama2_spec_test_"+"_".join(instruments)+".txt"
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
        res=SP[i].connect()
        if res!="OK":
            break

    try:

        #Set integration time:
        if res=="OK":
            logger.info("--- Setting IT ---")
            for i in range(len(SP)):
                res=SP[i].set_it(it_ms=10.0)
                if res!="OK":
                    break

        #Do testing Measurement:
        if res=="OK":
            logger.info("--- Starting measurements ---")
            for i in range(len(SP)):
                res=SP[i].measure(ncy=10)

        #Wait for measurement finished
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

        if res!="OK":
            logger.error("Error during testing: "+res)
    except Exception as e:
        logger.exception(e)


    #Finally, disconnect
    logger.info("--- Test Finished, disconnecting... ---")
    sleep(2)
    for i in range(len(SP)):
        res=SP[i].disconnect(dofree=True)

    logger.info("--- Finished ---")





