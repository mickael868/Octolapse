# coding=utf-8
from __future__ import absolute_import
import octoprint.plugin
import uuid
import time
import os
import sys
from .settings import OctolapseSettings
from .gcode import *
from .snapshot import CaptureSnapshot,SnapshotInfo
from .position import *
from octoprint.events import eventManager, Events
from .trigger import *
import itertools
from .utility import *
from .render import Render
import shutil
from .camera import CameraControl
class OctolapsePlugin(	octoprint.plugin.SettingsPlugin,
						octoprint.plugin.AssetPlugin,
						octoprint.plugin.TemplatePlugin,
						octoprint.plugin.StartupPlugin,
						octoprint.plugin.EventHandlerPlugin):
	TIMEOUT_DELAY = 1000
	IsStarted = False
	def __init__(self):
		self.CameraControl = None
		self.Camera = None
		self.OctolapseGcode = None
		self.CaptureSnapshot = None
		self.PrintStartTime = time.time()
		self.Settings = None
		self.Triggers = []
		self.Position = None
		self.IsPausedByOctolapse = False
		self.SnapshotGcode = None
		self.SavedCommand = None
		self.SnapshotCount = 0
		self._IsTriggering = False
		self.WaitForSnapshot = False
		self.Render = None
		self.IsRendering = False
		self.WaitForPosition=True;
		self.Responses = Responses();
		self.Commands = Commands();
		self.SendingCommands = False;
		self.CommandIndex = 0;
	##~~ After Startup
	def on_after_startup(self):
		self.reload_settings()
		self._logger.info("Octolapse - loaded and active.")
		IsStarted = True

	def reload_settings(self):
		if(self._settings is None):
			self._logger.error("The plugin settings (_settings) is None!")
			return
		self.Settings = OctolapseSettings(self._logger,self._settings)
		self.Camera = self.Settings.CurrentCamera()
		#self._logger.info("Octolapse - Octoprint settings converted to octolapse settings: {0}".format(settings.GetSettingsForOctoprint(self._logger,self.Settings)))
	##~~ SettingsPlugin mixin

	def on_settings_save(self, data):
		octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
		#for printer in self._settings.get(["printers"]):
		#	if(printer.get(["guid"]).startswith("NewPrinterGuid_")):
		#		newGuid = str(uuid.uuid4())
		#		if (self._settings.get(["current_printer_guid"]) == printer.guid):
		#			self._settings.set(["current_printer_guid"],newGuid)
		#		printer.guid = newGuid
		
		self.Settings.debug.LogSettingsSave('Settings Saved: {0}'.format(self._settings))
		
	def get_settings_defaults(self):
		defaultSettings = settings.GetSettingsForOctoprint(self._logger,None)
		self._logger.info("Octolapse - creating default settings: {0}".format(defaultSettings))
		return defaultSettings
	def get_template_configs(self):
		self._logger.info("Octolapse - is loading template configurations.")
		return [dict(type="settings", custom_bindings=True)]
	def CurrentPrinterProfile(self):
		return self._printer_profile_manager.get_current()
	## EventHandlerPlugin mixin
	def on_event(self, event, payload):

		if(self.Settings is None or not self.Settings.is_octolapse_enabled):
			return
		if (event == Events.PRINT_PAUSED):
			if(not self.IsPausedByOctolapse):
				self.OnPrintPause()
			else:
				self.OnPrintPausedByOctolapse()
		elif (event == Events.PRINT_RESUMED):
			self.OnPrintResumed()
		elif (event == Events.PRINT_STARTED):
			self.OnPrintStart()
		elif (event == Events.PRINT_FAILED):
			self.OnPrintFailed()
		elif (event == Events.PRINT_CANCELLED):
			self.OnPrintCancelled()
		elif (event == Events.PRINT_DONE):
			self._logger.info("Octolapse - Print Done")
			self.OnPrintCompleted()
	def ClearTriggers(self):
		self.Triggers[:] = []
	def OnPrintResumed(self):
		
		self.Settings.debug.LogPrintStateChange("Print Resumed.")
	def OnPrintPause(self):
		self.Settings.debug.LogPrintStateChange("Print Paused.")
		if(self.Triggers is not None and len(self.Triggers)>0):
			for trigger in self.Triggers:
				if(type(trigger) == TimerTrigger):
					trigger.Pause()
	def OnPrintPausedByOctolapse(self):
		self.Settings.debug.LogPrintStateChange("Print Paused by Octolapse.")
		self.GetPositionForSnapshotReturn()
	def OnPrintStart(self):
		self.reload_settings()
		self.Settings.debug.LogPrintStateChange("Octolapse - Print Started.")
		self.CameraControl = CameraControl(self.Settings)
		self.OctolapseGcode = Gcode(self.Settings,self.CurrentPrinterProfile())
		self.CaptureSnapshot = CaptureSnapshot(self.Settings)
		if(not self.IsRendering):
			self.CaptureSnapshot.CleanSnapshots(None,'before-print')
		self.ClearTriggers()
		self.Position = Position(self.Settings,self.CurrentPrinterProfile())
		self.Render = Render(self.Settings,1,self.OnRenderStart,self.OnRenderFail,self.OnRenderComplete,None)
		self.SnapshotCount = 0
		self.CaptureSnapshot.SetPrintStartTime(time.time())
		self.CaptureSnapshot.SetPrintEndTime(None)
		self.IsPausedByOctolapse = False
		self.WaitForSnapshot = False
		# create the triggers for this print
		snapshot = self.Settings.CurrentSnapshot()
		# If the gcode trigger is enabled, add it
		if(snapshot.gcode_trigger_enabled):
			#Add the trigger to the list
			self.Triggers.append(GcodeTrigger(self.Settings))
		# If the layer trigger is enabled, add it
		if(snapshot.layer_trigger_enabled):
			#Configure the extruder triggers
			
			self.Triggers.append(LayerTrigger(self.Settings))
		# If the layer trigger is enabled, add it
		if(snapshot.timer_trigger_enabled):
			#Configure the extruder triggers
			
			self.Triggers.append(TimerTrigger(self.Settings))
		if(self.Camera.apply_settings_before_print):
			self.CameraControl.ApplySettings()
	def OnPrintFailed(self):
		self.Settings.debug.LogPrintStateChange("Print Failed.")
		if(not self.IsRendering):
			self.Render.Process(self.CurrentlyPrintingFileName(),  self.CaptureSnapshot.PrintStartTime, self.CaptureSnapshot.PrintEndTime);
		if(not self.IsRendering):
			self.Settings.debug.LogInfo("Started Rendering Timelapse");
			self.CaptureSnapshot.CleanSnapshots(self.CurrentlyPrintingFileName(),'after-failed')
		self.OnPrintEnd()
	def OnPrintCancelled(self):
		self.Settings.debug.LogPrintStateChange("Print Cancelled.")
		if(not self.IsRendering):
			self.Settings.debug.LogInfo("Started Rendering Timelapse");
			self.Render.Process(self.CurrentlyPrintingFileName(),  self.CaptureSnapshot.PrintStartTime, self.CaptureSnapshot.PrintEndTime);
		if(not self.IsRendering):
			self.CaptureSnapshot.CleanSnapshots(self.CurrentlyPrintingFileName(),'after-cancel')
		self.OnPrintEnd()
	def OnPrintCompleted(self):
		self.CaptureSnapshot.SetPrintEndTime(time.time())
		if(not self.IsRendering):
			self.Settings.debug.LogInfo("Started Rendering Timelapse");
			self.Render.Process(self.CurrentlyPrintingFileName(),  self.CaptureSnapshot.PrintStartTime, self.CaptureSnapshot.PrintEndTime);
		
		self.Settings.debug.LogPrintStateChange("Print Completed!")
		if(not self.IsRendering):
			self.CaptureSnapshot.CleanSnapshots(self.CurrentlyPrintingFileName(),'after-print')
		self.OnPrintEnd()
	def OnPrintEnd(self):
		self.ClearTriggers()
		self.Position = None
	def OnRenderStart(self, *args, **kwargs):
		self.Settings.debug.LogRenderStart("Starting.")
		self.IsRendering = False
	def OnRenderComplete(self, *args, **kwargs):
		filePath = args[0]
		self.Settings.debug.LogRenderComplete("Completed rendering {0}.".format(args[0]))
		rendering = self.Settings.CurrentRendering()
		if(rendering.sync_with_timelapse):
			self.Settings.debug.LogRenderSync("Syncronizing timelapse with the built in timelapse plugin, copying {0} to {1}".format(filePath,rendering.octoprint_timelapse_directory ))
			try:
				shutil.move(filePath,rendering.octoprint_timelapse_directory)
			except:
				type = sys.exc_info()[0]
				value = sys.exc_info()[1]
				self.Settings.debug.LogError("Could move the timelapse at {0} to the octoprint timelaspse directory as {1}. Error Type:{2}, Details:{3}".format(filePath,rendering.octoprint_timelapse_directory,type,value))
		

		self.IsRendering = False
		self.CaptureSnapshot.CleanSnapshots(self.CurrentlyPrintingFileName(),'after_render_complete')
	def OnRenderFail(self, *args, **kwargs):
		self.IsRendering = False
		self.CaptureSnapshot.CleanSnapshots(self.CurrentlyPrintingFileName(),'after_render_fail')
		self.Settings.debug.LogRenderFail("Failed.")
	def CurrentlyPrintingFileName(self):
		if(self._printer is not None):
			current_job = self._printer.get_current_job()
			if current_job is not None and "file" in current_job:
				current_job_file = current_job["file"]
				if "path" in current_job_file and "origin" in current_job_file:
					current_file_path = current_job_file["path"]
					return utility.GetFilenameFromFullPath(current_file_path)
		return ""
	def GcodeQueuing(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
		# update the position tracker so that we know where all of the axis are.
		# We will need this later when generating snapshot gcode so that we can return to the previous
		# position
		#

		# check for assert commands
		if(self.Settings is not None):
			
			self.Settings.debug.ApplyCommands(cmd, triggers=self.Triggers, isSnapshot=self.IsPausedByOctolapse)

		if(self.Position is not None):
			self.Position.Update(cmd)
		# preconditions
		if (# wait for the snapshot command to finish sending, or wait for the snapshot delay in case of timeouts)
			self.Settings is None
			or not self.Settings.is_octolapse_enabled
			or self.Triggers is None
			or len(self.Triggers)<1
			or self._printer is None
			or self.IsPausedByOctolapse
			):
			return cmd
		currentTrigger = trigger.IsTriggering(self.Triggers,self.Position, cmd, self.Settings.debug)
		if(currentTrigger is not None):
			#We're triggering
			
			# build an array of commands to take the snapshot
			if(not self.IsPausedByOctolapse):
				self.SavedCommand = cmd;
				self.IsPausedByOctolapse = True
				self._printer.pause_print()
				return None
			else:
				self.Settings.debug.LogError("Cannot take a snapshot, there are no snapshot gcode commands to execute!  Check your profile settings or re-install.")

		if( trigger.IsSnapshotCommand(cmd,self.Settings.printer.snapshot_command)):
			cmd = None
		return cmd
	def GcodeSent(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
		if(self.Settings is None
			or not self.Settings.is_octolapse_enabled
			or self.Triggers is None
			or len(self.Triggers)<1
			or self._printer is None):
			return

		if(self.SendingCommands):
			sentCommand =self.SnapshotGcode.ByIndex(self.CommandIndex)
			self.Settings.debug.LogSnapshotDownload("Looking for snapshot command index {0}.  Command Send:{1}, Command Expected:{2}".format(self.CommandIndex, cmd, sentCommand))

			if(cmd == sentCommand):
				if(self.CommandIndex == self.SnapshotGcode.StartEndIndex() and not self.WaitForSnapshot):
					self.Settings.debug.LogSnapshotGcodeEndcommand("End Snapshot Gcode Command Found, waiting for snapshot.")
					self.WaitForSnapshot = True

				self.CommandIndex+=1

				if(self.CommandIndex >= self.SnapshotGcode.CommandCount()):
					self.Settings.debug.LogSnapshotDownload("Sent the final snapshot command, resuming print.")
					self.ResetSnapshotState()
					self._printer.resume_print()
			


	def GcodeReceived(self, comm, line, *args, **kwargs):
		if(self.IsPausedByOctolapse):
			if(self.WaitForSnapshot):
				self.WaitForSnapshot = False
				self.Settings.debug.LogSnapshotGcodeEndcommand("End wait for snapshot:{0}".format(line))
				self.TakeSnapshot()
			elif(self.WaitForPosition):
				self.ReceivePositionForSnapshotReturn(line)
		return line

	def ResetSnapshotState(self):
		self.IsPausedByOctolapse = False
		self.SnapshotGcode = None
		self.WaitForSnapshot = False
		
		self.WaitForPosition = False
		self.CommandIndex=0
		self.SendingCommands = False
		
		
	
	def GetPositionForSnapshotReturn(self):
		self.WaitForPosition=True;
		self._printer.commands(self.Commands.M114.Command);

	def ReceivePositionForSnapshotReturn(self, line):
		parsedResponse = self.Responses.M114.Parse(line)
		self.Settings.debug.LogSnapshotPositionReturn("Snapshot return position received - response:{0}, parsedResponse:{1}".format(line,parsedResponse))
		if(parsedResponse):

			x=parsedResponse["X"]
			y=parsedResponse["Y"]
			z=parsedResponse["Z"]
			e=parsedResponse["E"]

			self.Settings.debug.LogSnapshotPositionReturn("Snapshot return position received - x:{0},y:{1},z:{2},e:{3}".format(x,y,z,e))
			previousX = self.Position.X
			previousY = self.Position.Y
			previousZ = self.Position.Z
			if(previousX != x):
				self.Settings.debug.LogWarning("The position recieved from the printer does not match the position expected by Octolapse.  This could indicate a problem in the GCode, or a bug in octolapse's position tracking routine.")

			self.Position.UpdatePosition(x,y,z,e)
			
			self.SnapshotGcode = self.OctolapseGcode.GetSnapshotGcode(self.Position,self.Position.Extruder)
			if(self.SnapshotGcode is None):
				self.Settings.debug.LogError("No snapshot gcode was created for this snapshot.  Aborting this snapshot.")
				self.ResetSnapshotState();
				return;

			self.Settings.debug.LogSnapshotGcodeEndcommand("End Gcode Command:{0}".format(self.SnapshotGcode.ReturnEndCommand()))
			self.SnapshotGcode.SavedCommand = self.SavedCommand
			self.WaitForPosition = False
			self.SendSnapshotGcode()


	
	def SendSnapshotGcode(self):
		if(self.SnapshotGcode is None):
			self.Settings.debug.LogError("Cannot send snapshot Gcode, no gcode returned")

		returnCommands = self.SnapshotGcode.ReturnCommands
		savedCommand = self.SnapshotGcode.SavedCommand
		# Send commands to move to the snapshot position
		self.SendingCommands = True
		self._printer.commands(self.SnapshotGcode.StartCommands);
		# Start the return journey!
		self._printer.commands(returnCommands)
		self._printer.commands(savedCommand);
		

	def TakeSnapshot(self):
		snapshot = self.CaptureSnapshot
		self.SnapshotCount += 1
		if(snapshot is not None):
			try:
				snapshot.Snap(self.CurrentlyPrintingFileName(),self.SnapshotCount)
			except:
					
				a = sys.exc_info() # Info about unknown error that caused exception.                                              
				errorMessage = "    {0}".format(a)
				b = [ str(p) for p in a ]
				errorMessage += "\n    {0}".format(b)
				self._logger.error('Unknown error detected:{0}'.format(errorMessage))
			
		else:
			self.Settings.debug.LogError("Failed to retrieve the snapshot module!  It might work again later.")

	
	##~~ AssetPlugin mixin
	def get_assets(self):
		self._logger.info("Octolapse is loading assets.")
		# Define your plugin's asset files to automatically include in the
		# core UI here.
		return dict(js = ["js/octolapse.js"],
			css = ["css/octolapse.css"],
			less = ["less/octolapse.less"])

	##~~ Softwareupdate hook
	def get_update_information(self):
		# Define the configuration for your plugin to use with the Software Update
		# Plugin here.  See
		# https://github.com/foosel/OctoPrint/wiki/Plugin:-Software-Update
		# for details.
		self._logger.info("Octolapse is geting update information.")
		return dict(octolapse = dict(displayName="Octolapse Plugin",
				displayVersion=self._plugin_version,
				# version check: github repository
				type="github_release",
				user="FormerLurker",
				repo="Octolapse",
				current=self._plugin_version,
				# update method: pip
				pip="https://github.com/FormerLurker/Octolapse/archive/{target_version}.zip"))

# If you want your plugin to be registered within OctoPrint under a different
# name than what you defined in setup.py
# ("OctoPrint-PluginSkeleton"), you may define that here.  Same goes for the
# other metadata derived from setup.py that
# can be overwritten via __plugin_xyz__ control properties.  See the
# documentation for that.
__plugin_name__ = "Octolapse Plugin"

def __plugin_load__():
	global __plugin_implementation__
	__plugin_implementation__ = OctolapsePlugin()

	global __plugin_hooks__
	__plugin_hooks__ = {
		"octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
		"octoprint.comm.protocol.gcode.queuing": __plugin_implementation__.GcodeQueuing,
		"octoprint.comm.protocol.gcode.sent": __plugin_implementation__.GcodeSent,
		"octoprint.comm.protocol.gcode.received": __plugin_implementation__.GcodeReceived,

	}

