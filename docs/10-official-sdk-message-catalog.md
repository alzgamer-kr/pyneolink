# Official SDK Message Catalog

This catalog maps message IDs recovered from the official Windows SDK
or confirmed by decrypted live traffic to XML roots and named
`BCSDK_Remote*` entry points. It is reverse-engineering evidence, not a
statement that every camera supports every command or that PyNeolink
currently implements it.

A message ID can represent a command family shared by several internal
operations. `bccmd` is the SDK's internal command selector; it is not sent
as the Baichuan wire message ID.

Cataloged values: **194**. Unresolved values: **0**.

## IDs 20-149

| ID | XML root / family | Official operations | `bccmd` | Evidence |
|---:|---|---|---|---|
| 20 | `PtzCruise` | `CruiseInvoke`, `CruiseStop`, `SetCruise` | `0x851` | XML serializer, named SDK export |
| 24 | `Shutdown` | `Shutdown` | `0x839` | named SDK export, no-body SDK export |
| 25 | `InputAdvanceCfg, VideoInput` | `SetIspCfg`, `SetIspDayNightMode` | `0x7d4, 0x7de, 0x89b` | XML serializer, named SDK export |
| 34 | `Serial` | `SetPtzCfg` | `0x80a` | inline XML serializer, named SDK export |
| 39 | `Ntp` | `SetNtp` | `0x7fc` | XML serializer, named SDK export |
| 41 | `Ddns` | `SetDdns` | `0x7fe` | XML serializer, named SDK export |
| 45 | `OsdChannelName` | `SetOsdCfg` | `0x7d2` | XML serializer, named SDK export |
| 47 | `MD` | `SetMotionCfg` | `0x7d8` | XML serializer, named SDK export |
| 49 | `HideAlarm` | `-` | `0x7da` | XML serializer |
| 51 | `VideoLost` | `SetVideoLoss` | `0x7d6` | XML serializer, named SDK export |
| 53 | `Shelter` | `SetShelter` | `0x7dc` | XML serializer, named SDK export |
| 55 | `RecordCfg` | `SetRecordGenCfg` | `0x7ec` | XML serializer, named SDK export |
| 57 | `Compression` | `SetEncCfg` | `0x7e4` | XML serializer, named SDK export |
| 69 | `Ftp` | `SetFtpCfg` | `0x811` | XML serializer, named SDK export |
| 71 | `FtpTask` | `SetFtpTask` | `0x813` | XML serializer, named SDK export |
| 77 | `Dhcp` | `-` | `0x7f6` | inline XML serializer |
| 78 | `VideoInput` | `-` | `-` | live decrypted XML capture |
| 79 | `Serial` | `-` | `-` | live decrypted XML capture |
| 82 | `Record` | `SetRecordSchedule` | `0x7ee` | inline XML serializer, named SDK export |
| 84 | `HandleException` | `-` | `0x802` | XML serializer |
| 86 | `HandleException` | `SetHDDError` | `0x804` | XML serializer, named SDK export |
| 88 | `HandleException` | `SetNetDisconnect` | `0x806` | XML serializer, named SDK export |
| 90 | `HandleException` | `SetIpConflict` | `0x808` | XML serializer, named SDK export |
| 92 | `DisplayOutput` | `SetOutputCfg` | `0x7ea` | inline XML serializer, named SDK export |
| 98 | `Upnp` | `SetUpnpCfg` | `0x7f8` | XML serializer, named SDK export |
| 99 | `Restore` | `FactoryDefault` | `0x837` | inline XML serializer, named SDK export |
| 100 | `AutoReboot` | `SetAutoRebootCfg` | `0x80e` | inline XML serializer, named SDK export |
| 107 | `Dst` | `SetDst` | `0x7e0` | XML serializer, named SDK export |
| 117 | `Wifi` | `SetWifiCfg` | `0x81a` | XML serializer, named SDK export |
| 119 | `PasswordOnBoot` | `SetBootPwdState` | `0x820` | XML serializer, named SDK export |
| 121 | `OnlineUserList` | `SetOnlineUserCfg` | `0x817` | XML serializer, named SDK export |
| 125 | `PushInfo` | `-` | `0x83e` | inline XML serializer |
| 134 | `RfAlarm` | `SetDisarm`, `SetHomeArm`, `SetOutArm`, `SetSleepArm` | `0x840` | XML serializer, named SDK export |
| 149 | `IframePreviewList` | `BCSDK_SetDeviceIFramePreview` | `0x845` | inline XML serializer, named SDK export |

## IDs 150-297

| ID | XML root / family | Official operations | `bccmd` | Evidence |
|---:|---|---|---|---|
| 150 | `IframeReplayList` | `BCSDK_SetDeviceIFrameReplay` | `0x846` | inline XML serializer, named SDK export |
| 189 | `RequestIframe` | `BCSDK_RequestKeyFrame` | `0x9db` | XML serializer, named SDK export |
| 191 | `OnlineUpdate` | `OnlineUpate` | `0x84b` | XML serializer, named SDK export |
| 192 | `StartAlarmReport` | `StartAlarmReport` | `0x85e` | named SDK export, no-body SDK export |
| 193 | `StopAlarmReport` | `StopAlarmReport` | `0x85f` | named SDK export, no-body SDK export |
| 194 | `Ftp` | `SetFtpTest` | `0x856` | XML serializer, named SDK export |
| 196 | `AutoUpdate` | `SetAutoUpdateState` | `0x874` | XML serializer, named SDK export |
| 200 | `Wifi` | `WifiTest` | `0x855` | XML serializer, named SDK export |
| 207 | `CameraCfg` | `SetCameraCfg` | `0x866` | XML serializer, named SDK export |
| 211 | `PTOP` | `-` | `0x86a` | XML serializer |
| 218 | `PushTask` | `SetPushTask` | `0x87b` | XML serializer, named SDK export |
| 220 | `RtmpOpt` | `RtmpStart` | `0x876` | XML serializer, named SDK export |
| 221 | `RtmpOpt` | `RtmpStop` | `0x877` | XML serializer, named SDK export |
| 225 | `AutoFocus` | `SetAutoFocus` | `0x882` | XML serializer, named SDK export |
| 229 | `Crop` | `SetCropCfg` | `0x884` | XML serializer, named SDK export |
| 231 | `AudioTask` | `SetAudioTask` | `0x888` | XML serializer, named SDK export |
| 233 | `DeviceSleep` | `DeviceSleep` | `0x886` | named SDK export, no-body SDK export |
| 235 | `CloudTask` | `SetCloudTask` | `0x936` | XML serializer, named SDK export |
| 243 | `BaseDevOnlineList` | `BaseDeleteOnlineDevice` | `0x892` | XML serializer, named SDK export |
| 252 | `BatteryList` | `-` | `-` | live decrypted XML capture |
| 254 | `BaseWifiQRCode` | `SetBaseWifiQRCode` | `0x89c` | XML serializer, named SDK export |
| 258 | `Net3g4gModuleInfo` | `SetSimModuleInfo` | `0x8a0` | XML serializer, named SDK export |
| 262 | `audioFileInfo` | `SaveAudioFile`, `SaveRingtone` | `0x8a9, 0x904` | XML serializer, named SDK export |
| 265 | `audioCfg` | `SetRingtoneCfg` | `0x8ac` | XML serializer, named SDK export |
| 266 | `muteAudio` | `MuteAlarmAudio` | `0x8ad` | XML serializer, named SDK export |
| 269 | `BindCloud` | `BindCloud` | `0x8a3` | XML serializer, named SDK export |
| 271 | `CloudUploadCfg` | `SetCloudCfg` | `0x8a5` | XML serializer, named SDK export |
| 279 | `BindUnbindNas` | `NasBind` | `0x8b0` | XML serializer, named SDK export |
| 280 | `BindUnbindNas` | `NasUnbind` | `0x8b1` | XML serializer, named SDK export |
| 283 | `CloudLoginKey` | `SetSignatureLoginCfg` | `0x8b4` | XML serializer, named SDK export |
| 286 | `AgingTest` | `GetAgingTestResult` | `0x8f5` | XML serializer, named SDK export |
| 287 | `TimeCfg` | `SyncUtcTime` | `0x8b5` | XML serializer, named SDK export |
| 297 | `DayNightThreshold` | `SetDayNightThreshold` | `0x8c0` | XML serializer, named SDK export |

## IDs 300-498

| ID | XML root / family | Official operations | `bccmd` | Evidence |
|---:|---|---|---|---|
| 300 | `AiCfg` | `SetAiCfg` | `0x8c3` | XML serializer, named SDK export |
| 302 | `SmbCfg` | `SetSambaCfg` | `0x8c5` | XML serializer, named SDK export |
| 303 | `RecFilesDelete` | `BCSDK_DeleteRecFiles` | `0x8c6` | XML serializer, named SDK export |
| 305 | `NasUploadCfg` | `SetNasCfg` | `0x8c8` | XML serializer, named SDK export |
| 307 | `BuzzerTask` | `SetBuzzerTask` | `0x8ca` | XML serializer, named SDK export |
| 309 | `RecordEnable` | `SetRecordEnable` | `0x8cc` | XML serializer, named SDK export |
| 311 | `EmailEnable` | `SetEmailEnable` | `0x8ce` | XML serializer, named SDK export |
| 313 | `FtpEnable` | `SetFtpEnable` | `0x8d2` | XML serializer, named SDK export |
| 315 | `PushEnable` | `SetPushEnable` | `0x8d0` | XML serializer, named SDK export |
| 317 | `BuzzerEnable` | `SetBuzzerEnable` | `0x8d4` | XML serializer, named SDK export |
| 331 | `PtzGuard` | `SetGuard` | `0x8ef` | XML serializer, named SDK export |
| 337 | `FactoryTestMode` | `SetFactoryTestMode` | `0x8e5` | XML serializer, named SDK export |
| 339 | `FactoryTestInfo` | `SetFactoryTestInfo` | `0x8e7` | XML serializer, named SDK export |
| 343 | `AiDetectCfg` | `SetAIDetectCfg` | `0x8eb` | XML serializer, named SDK export |
| 345 | `AlarmArea` | `SetAlarmAreas` | `0x8ed` | XML serializer, named SDK export |
| 348 | `audioFileInfo` | `DeleteAudioFile` | `0x901` | XML serializer, named SDK export |
| 349 | `audioFileInfo` | `PlayAudioFile` | `0x902` | XML serializer, named SDK export |
| 353 | `WiFiBandwidthTest` | `WiFiBandwidthTest` | `0x8f2` | XML serializer, named SDK export |
| 354 | `MesTestInfo` | `SetMesTestInfo` | `0x8f3` | XML serializer, named SDK export |
| 364 | `PushCfg` | `SetPushConfig` | `0x8fb` | XML serializer, named SDK export |
| 367 | `CloudTest` | `CloudTest` | `0x8fe` | named SDK export, no-body SDK export |
| 368 | `audioFileInfoList` | `SetAudioFileList` | `0x900` | XML serializer, named SDK export |
| 370 | `FishEyeImage` | `SetFishEyeImageInfo` | `0x947` | XML serializer, named SDK export |
| 373 | `SmartPlugEnable` | `SetSmartPlugEnable` | `0x906` | XML serializer, named SDK export |
| 375 | `ScheduleList` | `SetSmartPlugSchedule` | `0x908` | XML serializer, named SDK export |
| 377 | `Timer` | `SetSmartPlugTimer` | `0x90a` | XML serializer, named SDK export |
| 379 | `SmartPlugJogSwitch` | `SetSmartPlugJogSwitch` | `0x90c` | XML serializer, named SDK export |
| 387 | `SmartPlugPowerOnState` | `SetSmartPlugPowerOnState` | `0x90f` | XML serializer, named SDK export |
| 391 | `IOTBindUnBind` | `IOTBind` | `0x914` | conditional XML serializer, named SDK export |
| 392 | `IOTBindUnBind` | `IOTUnbind` | `0x915` | conditional XML serializer, named SDK export |
| 395 | `IOTAction` | `SetIOTAction` | `0x918` | XML serializer, named SDK export |
| 397 | `Uid` | `SetUidInfo` | `0x91a` | XML serializer, named SDK export |
| 402 | `IOInCfg` | `SetIOInCfg` | `0x927` | XML serializer, named SDK export |
| 404 | `IOOutCfg` | `SetIOOutCfg` | `0x929` | XML serializer, named SDK export |
| 406 | `IOOutTask` | `SetIOOutTask` | `0x92b` | XML serializer, named SDK export |
| 409 | `UpdateGPS` | `UpdateGPS` | `0x922` | named SDK export, no-body SDK export |
| 416 | `StitchAndDc` | `-` | `0x92f` | XML serializer |
| 418 | `BinoStitch` | `SetBinoStitchCfg` | `0x92d` | XML serializer, named SDK export |
| 424 | `PowerSaving` | `SetPowerSavingTask` | `0x934` | XML serializer, named SDK export |
| 428 | `AutoReply` | `SetAutoReply` | `0x93b` | XML serializer, named SDK export |
| 431 | `GDPRCfg` | `SetGDPRCfg` | `0x938` | XML serializer, named SDK export |
| 435 | `trackLimit` | `SetSmartTrackLimit` | `0x93d` | XML serializer, named SDK export |
| 437 | `trackSchedule` | `SetSmartTrackTask` | `0x941` | XML serializer, named SDK export |
| 440 | `aiDenoise` | `SetAIDenoiseCfg` | `0x943` | XML serializer, named SDK export |
| 441 | `deviceDetect` | `DeviceDetect` | `0x95d` | XML serializer, named SDK export |
| 442 | `ftpServerOption` | `FTPServerOption` | `0x956` | XML serializer, named SDK export |
| 444 | `fishEyeCfg` | `SetFishEyeCfg` | `0x945` | XML serializer, named SDK export |
| 445 | `Ptz3DLocation` | `SetPtz3DLocation` | `0x946` | XML serializer, named SDK export |
| 447 | `ftpServerCfg` | `SetFTPServerCfg` | `0x958` | XML serializer, named SDK export |
| 450 | `largeBattery` | `SetLargeBatteryInfo` | `0x949` | XML serializer, named SDK export |
| 452 | `powerMode` | `SetPowerMode` | `0x94b` | XML serializer, named SDK export |
| 454 | `afAlgorithm` | `SetAFAlgorithm` | `0x94d` | XML serializer, named SDK export |
| 456 | `certificateInfo` | `SetHttpsCertInfo` | `0x94f` | XML serializer, named SDK export |
| 463 | `ropCfg` | `SetRopCfg` | `0x950` | XML serializer, named SDK export |
| 467 | `nasAuthInfo` | `NasAuthAccess` | `0x95b` | XML serializer, named SDK export |
| 468 | `ptzCurPos` | `SetPtzPostion` | `0x995` | XML serializer, named SDK export |
| 470 | `previewReplayLimit` | `SetPreviewReplayLimitCfg` | `0x955` | XML serializer, named SDK export |
| 472 | `nasAuthInfo` | `NasDeauth` | `0x95c` | XML serializer, named SDK export |
| 480 | `photoRecord` | `SetPhotoRecordCfg` | `0x960` | XML serializer, named SDK export |
| 482 | `kitApCfg` | `SetApCfg`, `SetSceneCfg` | `0x962` | XML serializer, named SDK export |
| 483 | `dingdongCtrl` | `DingDongCtrl` | `0x963` | XML serializer, named SDK export |
| 485 | `dingdongDeviceOpt` | `DingDongManualRing`, `DingDongPair`, `DingDongUnpair`, `SetDingDongOption` | `0x967, 0x968, 0x969, 0x96a` | XML serializer, named SDK export |
| 487 | `dingdongCfg` | `SetDingDongCfg` | `0x96c` | XML serializer, named SDK export |
| 497 | `devManage` | `ManageDevice` | `0x983` | XML serializer, conditional XML serializer, named SDK export |
| 498 | `devManage` | `ManageDevice` | `0x983` | XML serializer, conditional XML serializer, named SDK export |

## IDs 501-695

| ID | XML root / family | Official operations | `bccmd` | Evidence |
|---:|---|---|---|---|
| 501 | `backupStartInfo` | `StartBackup` | `0x975` | XML serializer, named SDK export |
| 503 | `CancelBackup` | `CancelBackup` | `0x977` | named SDK export, no-body SDK export |
| 505 | `DeiveAudioCfg` | `SetDeviceAudioCfg` | `0x979` | XML serializer, named SDK export |
| 506 | `RecEncCfg` | `SetRecEncCfg` | `0x989` | XML serializer, named SDK export |
| 508 | `RecDecKey` | `RecTryDec` | `0x98b` | XML serializer, named SDK export |
| 509 | `authInfo` | `GetLoginAuthCode` | `0x980` | XML serializer, named SDK export |
| 512 | `accessUserList` | `SetAccessUserCfg` | `0x982` | XML serializer, named SDK export |
| 518 | `findEventLog` | `-` | `0x987` | XML serializer |
| 521 | `aiSnapTlps` | `SetAISnaptlpsCfg` | `0x97b` | XML serializer, named SDK export |
| 524 | `PushCreateListener` | `CreatePushListener` | `0x97e` | XML serializer, named SDK export |
| 525 | `SyncPushClientList` | `SyncPushClientList` | `0x97f` | named SDK export, no-body SDK export |
| 528 | `CrosslineDetect` | `SetSmartAICrosslineDetectCfg` | `0x98d` | XML serializer, named SDK export |
| 530 | `IntrusionDetect` | `SetSmartAIIntrusionDetectCfg` | `0x98f` | XML serializer, named SDK export |
| 532 | `LoiteringDetect` | `SetSmartAILoiteringDetectCfg` | `0x991` | XML serializer, named SDK export |
| 535 | `T1MicTest` | `StartMicTest` | `0xa0b` | inline XML serializer, named SDK export |
| 538 | `WifiSDB, expandWifi` | `SetExpandWifiCfg`, `SetWifiSdbCfg` | `0x99b, 0x9b4` | XML serializer, named SDK export |
| 539 | `backupResume` | `ResumeBackup` | `0x993` | XML serializer, named SDK export |
| 540 | `backupTlpsInfo` | `BackupTlps` | `0x994` | XML serializer, named SDK export |
| 541 | `fishEyeSubChnCtrl` | `SetFishSubChnCtrl` | `0x997` | XML serializer, named SDK export |
| 544 | `ThirdCloudCfg, ThirdCloudTask` | `Set3rdCloudCfg`, `Set3rdCloudSchedule` | `0x9c5, 0x9c7` | XML serializer, named SDK export |
| 545 | `trackShelter` | `MoveToTrackArea` | `0x998` | XML serializer, named SDK export |
| 546 | `3rdCloudTest` | `3rdCloudTest` | `0x9c8` | named SDK export, no-body SDK export |
| 547 | `SirenStatusList` | `-` | `-` | live decrypted XML capture |
| 550 | `LegacyDetect` | `SetSmartAILegacyDetectCfg` | `0x99d` | XML serializer, named SDK export |
| 552 | `LossDetect` | `SetSmartAILossDetectCfg` | `0x99f` | XML serializer, named SDK export |
| 557 | `USBDevicePartId` | `FormatUsbDevice` | `0x9a0` | XML serializer, named SDK export |
| 560 | `captureModeCfg` | `SetCaptureModeCfg` | `0x9a7` | XML serializer, named SDK export |
| 562 | `timingCaptureCfg` | `SetTimingCaptureCfg` | `0x9a9` | XML serializer, named SDK export |
| 572 | `mobileTraceCfg` | `SetMobileTraceCfg` | `0x9ab` | XML serializer, named SDK export |
| 575 | `sleepState` | `SetSleepPosition`, `SetSleepState` | `0x9a4, 0x9a5` | XML serializer, named SDK export |
| 577 | `snaptlpsEnableCfg` | `SetSnaptlpsEnableCfg` | `0x9ae` | XML serializer, named SDK export |
| 584 | `gpsCfg` | `SetGpsCfg` | `0x9b0` | XML serializer, named SDK export |
| 587 | `manualRec` | `ManualRecord` | `0x9b1` | inline XML serializer, named SDK export |
| 595 | `longRunModeCfg` | `SetPreRecordCfg` | `0x9b6` | XML serializer, named SDK export |
| 602 | `sceneModeCfg` | `SetSceneModeCfg` | `0x9be` | XML serializer, named SDK export |
| 605 | `sceneCfg` | `-` | `0x9c1` | XML serializer |
| 608 | `FloodlightScheduleListV2` | `SetFloodlightTask_V2` | `0x9cb` | XML serializer, named SDK export |
| 610 | `dingdongSilentMode` | `SetDingDongSilentCfg` | `0x9c3` | XML serializer, named SDK export |
| 613 | `linkageAdd` | `AddLinkage` | `0x9d4` | XML serializer, named SDK export |
| 614 | `linkageDel` | `DeleteLinkage` | `0x9d5` | XML serializer, named SDK export |
| 615 | `linkageUpdate` | `UpdateLinkage` | `0x9d6` | XML serializer, named SDK export |
| 618 | `dcOutPut` | `SetDcOutputCfg` | `0x9ce` | XML serializer, named SDK export |
| 620 | `answerEvent` | `StartAnswerEvent` | `0x9d0` | XML serializer, named SDK export |
| 625 | `ThirdAuth` | `Set3rdClientAuthParam` | `0x9e5` | inline XML serializer, named SDK export |
| 627 | `BatteryMode` | `SetBatteryMode` | `0x9dd` | XML serializer, named SDK export |
| 629 | `YoloWorldCfgList` | `SetYoloWorldDetectCfg` | `0x9fa` | inline XML serializer, named SDK export |
| 631 | `YoloWorldTypeList` | `SetYoloWorldTypeList` | `0x9fc` | inline XML serializer, named SDK export |
| 638 | `DongleLargeBatteryInfo` | `SetDongleLargeBatteryInfo` | `0x9e1` | inline XML serializer, named SDK export |
| 643 | `imageSummary` | `BCSDK_ImageSummaryClose` | `0x9ff` | inline XML serializer, named SDK export |
| 647 | `intelligentAnswer` | `BCSDK_IntelligentAnswerClose` | `0xa02` | inline XML serializer, named SDK export |
| 650 | `DeleteRecordFile` | `DeleteRecordByTime` | `0x9ee` | inline XML serializer, named SDK export |
| 652 | `WpsMode` | `StartWpsMode` | `0x9e8` | inline XML serializer, named SDK export |
| 656 | `aiExtendRecord` | `SetAIExtRecCfg` | `0x9ec` | inline XML serializer, named SDK export |
| 660 | `dongleSub1gCtrl` | `DongleSub1gCtrl` | `0x9ef` | inline XML serializer, named SDK export |
| 662 | `dongleSub1gDevOpt` | `DonglePair`, `DongleUnPair` | `0x9f1, 0x9f2` | inline XML serializer, named SDK export |
| 665 | `IFTTTAdd` | `AddLinkageIPC` | `0x9f5` | inline XML serializer, named SDK export |
| 666 | `IFTTTDel` | `DeleteLinkageIPC` | `0x9f6` | inline XML serializer, named SDK export |
| 667 | `IFTTTUpdate` | `UpdateLinkageIPC` | `0x9f7` | inline XML serializer, named SDK export |
| 679 | `textFeature` | `SetTextFeature` | `0xa06` | inline XML serializer, named SDK export |
| 683 | `hybridge` | `SetHybridgeCfg` | `0xa08` | inline XML serializer, named SDK export |
| 684 | `DongleLargeBatteryStatus` | `SetDongleLargeBatteryStatus` | `0xa09` | inline XML serializer, named SDK export |
| 695 | `pirDtectCfg` | `SetPirMotionDetectCfg` | `0xa11` | inline XML serializer, named SDK export |

## Evidence Rules

- **XML serializer**: the dispatcher callback leads to a serializer
  containing the listed XML root.
- **Inline XML serializer**: the callback object is constructed directly
  in the dispatcher rather than by a reusable factory function.
- **Named SDK export**: a `BCSDK_Remote*` export queues the associated
  internal `bccmd`.
- **No-body SDK export**: the request has no XML serializer, so its name
  comes from the exported SDK operation and dispatcher branch.
- **Live decrypted XML capture**: a dispatcher trace contained a complete,
  readable XML payload with the listed root for that wire message ID.

Runtime captures are still required to establish direction, response
shape, model support, and safe public API behavior.
