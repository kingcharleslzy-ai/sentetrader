import Foundation
import CoreBluetooth
import IOKit.hid

// Documented IOHID event-service API (Apple TN2450); CLT's Swift module omits these declarations.
@_silgen_name("IOHIDEventSystemClientCreate") private func eventClient(_ allocator: CFAllocator?) -> Unmanaged<CFTypeRef>?
@_silgen_name("IOHIDEventSystemClientCopyServices") private func services(_ client: CFTypeRef) -> Unmanaged<CFArray>?
@_silgen_name("IOHIDServiceClientCopyProperty") private func property(_ service: CFTypeRef, _ key: CFString) -> Unmanaged<CFTypeRef>?
@_silgen_name("IOHIDServiceClientSetProperty") private func setProperty(_ service: CFTypeRef, _ key: CFString, _ value: CFTypeRef) -> Bool

private let serviceUUID = CBUUID(string: "AB5E0001-5A21-4F05-BC7D-AF01F617B664")
private let txUUID = CBUUID(string: "AB5E0002-5A21-4F05-BC7D-AF01F617B664")
private let audioUUID = CBUUID(string: "AB5E0003-5A21-4F05-BC7D-AF01F617B664")
private let controlUUID = CBUUID(string: "AB5E0004-5A21-4F05-BC7D-AF01F617B664")
private let keys: [UInt32: String] = [0x52:"up",0x51:"down",0x50:"left",0x4f:"right",0x28:"ok",0xf1:"back",0x4a:"home",0x65:"menu",0x35:"tv",0x80:"volumeUp",0x81:"volumeDown",0x3e:"voice",0x66:"power"]
private typealias Callback = @convention(c) (UnsafePointer<CChar>) -> Void
private var bridge: RemoteBridge?
private func onMain(_ work: @escaping () -> Void) { if Thread.isMainThread { work() } else { DispatchQueue.main.async(execute: work) } }

@_cdecl("ai_trader_remote_init") func remoteInit(_ callback: @escaping @convention(c) (UnsafePointer<CChar>) -> Void) {
    onMain { if bridge == nil { bridge = RemoteBridge(callback) } }
}
@_cdecl("ai_trader_remote_connect") func remoteConnect() { onMain { bridge?.connect() } }
@_cdecl("ai_trader_remote_disconnect") func remoteDisconnect() { onMain { bridge?.stop() } }
@_cdecl("ai_trader_remote_focus") func remoteFocus(_ focused: Bool) { onMain { bridge?.setFocus(focused) } }
@_cdecl("ai_trader_remote_write") func remoteWrite(_ bytes: UnsafePointer<UInt8>, _ count: Int) {
    let data = Data(bytes: bytes, count: count)
    onMain { bridge?.write(data) }
}

private final class RemoteBridge: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    private let callback: Callback
    private var central: CBCentralManager?
    private var peripheral: CBPeripheral?
    private var disconnecting: CBPeripheral?
    private var tx: CBCharacteristic?
    private var audio: CBCharacteristic?
    private var control: CBCharacteristic?
    private var service: CBService?
    private var notifications = Set<CBUUID>()
    private var enabled = false
    private var focused = false
    private var hid: IOHIDManager?
    private var hidServices: [(CFTypeRef, NSArray)] = []
    private var pressed = Set<UInt32>()
    private var writes: [Data] = []
    private var writing = false
    private let client = eventClient(kCFAllocatorDefault)?.takeRetainedValue()
    private let backupKey = "xiaomiRemote.keyMappingBackup.v1"
    private let mapKey = "UserKeyMapping" as CFString
    private let srcKey = "HIDKeyboardModifierMappingSrc"
    private let dstKey = "HIDKeyboardModifierMappingDst"

    init(_ callback: @escaping Callback) { self.callback = callback; super.init(); restoreResidualMapping() }
    private func emit(_ value: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: value), let text = String(data: data, encoding: .utf8) else { return }
        text.withCString { callback($0) }
    }
    private func error(_ message: String) { emit(["kind":"error", "message":message]) }
    func connect() {
        stop(); enabled = true
        if central == nil { central = CBCentralManager(delegate: self, queue: .main) }
        else if central?.state == .poweredOn && disconnecting == nil { discover() }
        else { centralManagerDidUpdateState(central!) }
    }
    func stop() {
        enabled = false; stopKeys(); central?.stopScan()
        // Detach first: late callbacks from a replaced connection cannot enter its successor.
        peripheral?.delegate = nil
        if let p = peripheral, p.state != .disconnected { disconnecting = p; central?.cancelPeripheralConnection(p) }
        peripheral = nil; tx = nil; audio = nil; control = nil; service = nil
        notifications.removeAll(); writes.removeAll(); writing = false
    }
    func setFocus(_ value: Bool) { focused = value; if value && enabled && tx != nil { startKeys() } else { stopKeys() } }
    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        guard enabled else { return }
        if central.state == .poweredOn {
            if disconnecting?.state == .disconnected { disconnecting = nil }
            if disconnecting == nil { discover() }
        }
        else if central.state == .unauthorized { error("请在系统设置 → 隐私与安全性 → 蓝牙中允许 Sente") }
        else if central.state == .poweredOff { error("请打开 Mac 蓝牙后重新连接遥控器") }
        else if central.state == .unsupported { error("这台 Mac 不支持低功耗蓝牙") }
    }
    private func discover() {
        guard let central else { return }
        let known = central.retrieveConnectedPeripherals(withServices: [serviceUUID]).filter { validName($0.name) }
        if known.count == 1 { select(known[0]); return }
        if known.count > 1 { error("发现多个小米遥控器，请仅保留一个连接"); return }
        // Paired Xiaomi firmware may omit the ATVV UUID from advertising. Filter names on receipt.
        central.scanForPeripherals(withServices: nil, options: [CBCentralManagerScanOptionAllowDuplicatesKey:false])
    }
    private func validName(_ name: String?) -> Bool { name == "小米蓝牙语音遥控器" || name == "Mi RC" }
    private func select(_ p: CBPeripheral) { central?.stopScan(); peripheral = p; p.delegate = self; central?.connect(p) }
    func centralManager(_ central: CBCentralManager, didDiscover p: CBPeripheral, advertisementData: [String: Any], rssi RSSI: NSNumber) {
        guard enabled, peripheral == nil, validName(p.name ?? advertisementData[CBAdvertisementDataLocalNameKey] as? String) else { return }; select(p)
    }
    func centralManager(_ central: CBCentralManager, didConnect p: CBPeripheral) { guard enabled, p == peripheral else { return }; p.discoverServices([serviceUUID]) }
    func centralManager(_ central: CBCentralManager, didFailToConnect p: CBPeripheral, error: Error?) { guard p == peripheral else { return }; self.error("遥控器蓝牙连接失败，请检查配对") }
    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral p: CBPeripheral, error: Error?) {
        if p == disconnecting { disconnecting = nil; if enabled { discover() }; return }
        guard enabled, p == peripheral else { return }; stopKeys(); emit(["kind":"disconnected"])
    }
    func peripheral(_ p: CBPeripheral, didDiscoverServices error: Error?) {
        guard enabled, p == peripheral else { return }
        guard error == nil, let service = p.services?.first(where: { $0.uuid == serviceUUID }) else { self.error("遥控器未提供 ATVV 语音服务"); return }
        self.service = service; p.discoverCharacteristics([txUUID,audioUUID,controlUUID], for: service)
    }
    func peripheral(_ p: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        guard enabled, p == peripheral, service === self.service else { return }
        guard error == nil, let chars = service.characteristics,
            let tx = chars.first(where: { $0.uuid == txUUID }), tx.properties.contains(.write),
            let audio = chars.first(where: { $0.uuid == audioUUID }), audio.properties.contains(.notify),
            let control = chars.first(where: { $0.uuid == controlUUID }), control.properties.contains(.notify)
        else { self.error("遥控器语音通道不完整"); return }
        self.tx = tx; self.audio = audio; self.control = control
        p.setNotifyValue(true, for: audio); p.setNotifyValue(true, for: control)
    }
    func peripheral(_ p: CBPeripheral, didUpdateNotificationStateFor c: CBCharacteristic, error: Error?) {
        guard enabled, p == peripheral, c === audio || c === control else { return }
        guard error == nil, c.isNotifying else { self.error("遥控器音频通知订阅失败"); return }
        notifications.insert(c.uuid)
        if notifications.contains(audioUUID) && notifications.contains(controlUUID) {
            if focused && !startKeys() { return }; emit(["kind":"connected", "name":p.name ?? "米家遥控器 2 Pro"])
        }
    }
    func peripheral(_ p: CBPeripheral, didUpdateValueFor c: CBCharacteristic, error: Error?) {
        guard enabled, p == peripheral, c === audio || c === control, notifications.contains(c.uuid) else { return }
        guard error == nil, let data = c.value else { self.error("遥控器数据读取失败"); return }
        emit(["kind":c.uuid == audioUUID ? "audio" : "control", "bytes":Array(data)])
    }
    func write(_ data: Data) {
        guard enabled, peripheral?.state == .connected, tx != nil, data.count <= 16, writes.count < 8 else { error("遥控器控制通道不可用"); return }
        writes.append(data); nextWrite()
    }
    private func nextWrite() { guard !writing, let tx, !writes.isEmpty else { return }; writing = true; peripheral?.writeValue(writes.removeFirst(), for: tx, type: .withResponse) }
    func peripheral(_ p: CBPeripheral, didWriteValueFor c: CBCharacteristic, error: Error?) {
        guard enabled, p == peripheral, c === tx else { return }; writing = false
        if error != nil { self.error("遥控器控制命令写入失败") } else { nextWrite() }
    }

    // Only this VID/PID's keyboard event services are remapped. Ordinary keyboards never participate.
    private func remoteServices() -> [CFTypeRef] {
        guard let client, let list = services(client)?.takeRetainedValue() as? [CFTypeRef] else { return [] }
        return list.filter { (property($0,"VendorID" as CFString)?.takeRetainedValue() as? NSNumber)?.intValue == 0x2717
            && (property($0,"ProductID" as CFString)?.takeRetainedValue() as? NSNumber)?.intValue == 0x32b8
            && (property($0,"PrimaryUsagePage" as CFString)?.takeRetainedValue() as? NSNumber)?.intValue == 1
            && (property($0,"PrimaryUsage" as CFString)?.takeRetainedValue() as? NSNumber)?.intValue == 6 }
    }
    private var mutedMap: NSArray { keys.keys.filter { $0 != 0xf1 }.sorted().map { [srcKey:UInt64(0x700000000)+UInt64($0), dstKey:UInt64(0x700000070)] } as NSArray }
    private func restoreResidualMapping() {
        guard let backup = UserDefaults.standard.array(forKey: backupKey) as? [[String: Any]] else { return }
        for service in remoteServices() {
            if let current = property(service,mapKey)?.takeRetainedValue() as? NSArray, current == mutedMap {
                _ = setProperty(service,mapKey,backup as NSArray)
            }
        }
    }
    @discardableResult private func startKeys() -> Bool {
        guard hid == nil else { return true }
        restoreResidualMapping()
        if #available(macOS 10.15, *) {
            guard IOHIDCheckAccess(kIOHIDRequestTypeListenEvent) == kIOHIDAccessTypeGranted else {
                _ = IOHIDRequestAccess(kIOHIDRequestTypeListenEvent)
                error("请允许 Sente 输入监控后重新连接遥控器"); return false
            }
        } else {
            error("遥控器按键需要 macOS 10.15 或更新版本"); return false
        }
        let targets = remoteServices()
        guard targets.count == 1 else { error("未找到唯一遥控键盘，请检查系统配对后重试"); return false }
        let service = targets[0]
        let previous = property(service,mapKey)?.takeRetainedValue() as? NSArray ?? []
        // Refuse to overwrite another remapper's configuration; its owner must release it first.
        guard previous.count == 0 else { error("遥控器已有按键映射，请先关闭其他遥控软件后重试"); return false }
        UserDefaults.standard.set(previous, forKey: backupKey)
        guard setProperty(service,mapKey,mutedMap), (property(service,mapKey)?.takeRetainedValue() as? NSArray) == mutedMap else {
            if (property(service,mapKey)?.takeRetainedValue() as? NSArray) == mutedMap { _ = setProperty(service,mapKey,previous) }
            error("无法安全接管遥控按键，请重新配对后重试"); return false
        }
        hidServices = [(service,previous)]
        let manager = IOHIDManagerCreate(kCFAllocatorDefault, 0)
        hid = manager
        IOHIDManagerSetDeviceMatching(manager,[kIOHIDVendorIDKey:0x2717,kIOHIDProductIDKey:0x32b8] as CFDictionary)
        IOHIDManagerRegisterInputValueCallback(manager, { context, result, _, value in
            guard result == kIOReturnSuccess, let context else { return }
            let owner = Unmanaged<RemoteBridge>.fromOpaque(context).takeUnretainedValue()
            let element = IOHIDValueGetElement(value)
            guard IOHIDElementGetUsagePage(element) == 7 else { return }
            let usage = IOHIDElementGetUsage(element)
            guard let key = keys[usage] else { return }
            let down = IOHIDValueGetIntegerValue(value) != 0
            if down { guard owner.pressed.insert(usage).inserted else { return } }
            else { guard owner.pressed.remove(usage) != nil else { return } }
            owner.emit(["kind":"key","key":key,"pressed":down])
        }, Unmanaged.passUnretained(self).toOpaque())
        IOHIDManagerScheduleWithRunLoop(manager,CFRunLoopGetMain(),CFRunLoopMode.commonModes.rawValue)
        guard IOHIDManagerOpen(manager,0) == kIOReturnSuccess else {
            stopKeys(); error("遥控按键监听失败，请检查输入监控权限后重试"); return false
        }
        emit(["kind":"keysReady"]); return true
    }
    private func stopKeys() {
        if let hid { IOHIDManagerRegisterInputValueCallback(hid,nil,nil); IOHIDManagerUnscheduleFromRunLoop(hid,CFRunLoopGetMain(),CFRunLoopMode.commonModes.rawValue); IOHIDManagerClose(hid,0) }
        hid = nil; pressed.removeAll()
        for (service,previous) in hidServices {
            // Do not erase a new mapping written by somebody else while this app was active.
            if let current = property(service,mapKey)?.takeRetainedValue() as? NSArray, current == mutedMap {
                if !setProperty(service,mapKey,previous) { emit(["kind":"keyError","message":"遥控器原按键映射恢复失败，请关闭并重新打开 Sente 后检查"] ) }
            }
        }
        hidServices.removeAll()
    }
}
