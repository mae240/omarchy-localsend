import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import "Model.js" as Model

// Everything the LocalSend panel shows, kept outside the lazily built panel
// content so the bar icon is right before the panel first opens: the own
// receiver's state, the devices on the network, favorites, and the one
// transfer that may be running. All network work happens in
// bin/localsend-helper.py; this file only launches it and reads its JSON lines.
Item {
  id: root

  property var settings: ({})
  property string moduleName: ""
  property var shell: null
  property var strings: null
  property bool panelOpen: false

  readonly property string helperPath: String(Qt.resolvedUrl("bin/localsend-helper.py")).replace(/^file:\/\//, "")
  readonly property var helperCommand: ["python3", "-I", helperPath]
  readonly property int scanIntervalSec: intSetting("scanIntervalSec", 20, 5, 300)

  // Own device.
  property string alias: ""
  property string fingerprint: ""
  property int port: 53317
  property bool receiving: false
  property bool appInstalled: true
  property var ips: []
  property bool statusLoaded: false
  // Optimistic receiver state while a start or stop is still taking effect:
  // -1 follows reality, 0 or 1 is what was just asked for.
  property int _desiredReceiving: -1
  readonly property bool receiverActive: _desiredReceiving === -1 ? receiving : _desiredReceiving === 1
  readonly property bool receiverBusy: _desiredReceiving !== -1
  readonly property string receiverAddress: (ips.length > 0 ? String(ips[0]) : "") + ":" + port

  // Devices on the network, and the favorites remembered in shell.json.
  property var discovered: []
  property var _scanDevices: []
  property bool scanning: false
  property bool scannedOnce: false
  readonly property var favorites: Model.sanitizeFavorites(settings ? settings.favorites : [])
  readonly property var devices: Model.mergeDevices(discovered, favorites)
  readonly property int onlineCount: {
    var n = 0
    for (var i = 0; i < devices.length; i++) if (devices[i].online) n++
    return n
  }

  // The running transfer.
  property bool sending: false
  property var sendTarget: null
  property string sendPhase: ""
  property var progress: null
  property bool pickingFiles: false
  property var _pickTarget: null
  property bool _sendFinished: false

  property string actionStatus: ""
  property string lastError: ""

  function t(key) { return strings ? strings.t(key) : key }

  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function intSetting(name, fallback, min, max) {
    var n = parseInt(String(setting(name, fallback)), 10)
    if (!isFinite(n)) n = fallback
    return Math.max(min, Math.min(max, n))
  }

  // ---------------------------------------------------------------- status

  function refreshStatus() {
    if (statusProcess.running) return
    statusProcess.command = helperCommand.concat(["status"])
    statusProcess.running = true
  }

  function applyStatus(raw) {
    var lines = String(raw || "").split("\n")
    for (var i = lines.length - 1; i >= 0; i--) {
      var event = Model.parseEvent(lines[i])
      if (!event || event.event !== "status") continue
      alias = String(event.alias || "").substring(0, 64)
      fingerprint = String(event.fingerprint || "").substring(0, 128)
      port = parseInt(String(event.port), 10) || 53317
      receiving = event.receiving === true
      appInstalled = event.appInstalled !== false
      ips = event.ips instanceof Array ? event.ips.slice(0, 8) : []
      statusLoaded = true
      if (_desiredReceiving !== -1 && receiving === (_desiredReceiving === 1)) _desiredReceiving = -1
      return
    }
  }

  function toggleReceiver() {
    if (receiverBusy) return
    if (receiverActive) stopReceiver()
    else startReceiver()
  }

  function startReceiver() {
    if (!appInstalled) {
      lastError = t("localsend.appMissing")
      return
    }
    lastError = ""
    _desiredReceiving = 1
    // Its own user unit, so stopping it later is one systemctl call and the
    // app never ends up a child of the shell.
    Quickshell.execDetached(["systemd-run", "--user", "--quiet", "--collect", "--unit", "mae-localsend-receiver", "localsend", "--hidden"])
    settleTimer.restart()
    giveUpTimer.restart()
  }

  function stopReceiver() {
    _desiredReceiving = 0
    // Also ends a LocalSend the user started by hand: the switch says
    // "receiving", and off is off.
    Quickshell.execDetached(["bash", "-c", "systemctl --user stop mae-localsend-receiver.service 2>/dev/null; pkill -x localsend_app"])
    settleTimer.restart()
    giveUpTimer.restart()
  }

  function openApp() {
    if (!appInstalled) {
      lastError = t("localsend.appMissing")
      return
    }
    // LocalSend runs once per user; launching it again raises the window of
    // the hidden receiver.
    Quickshell.execDetached(["uwsm-app", "--", "localsend"])
  }

  // -------------------------------------------------------------- discovery

  function rescan() {
    if (discoverProcess.running) return
    _scanDevices = []
    scanning = true
    discoverProcess.command = helperCommand.concat(["discover", "--timeout", "2.5"])
    discoverProcess.running = true
    discoverWatchdog.restart()
  }

  function handleDiscoverLine(line) {
    var event = Model.parseEvent(line)
    if (!event) return
    if (event.event === "device") {
      var device = Model.normalizeDevice(event)
      if (!device) return
      // A later event for the same device (now verified) replaces the
      // earlier one; the cap holds back new devices only.
      var next = Model.addCapped(_scanDevices, device, 200)
      if (next === null) return
      // An entry the newcomer pushed out leaves the visible list at once.
      var shown = Model.withoutDropped(discovered, _scanDevices, next)
      _scanDevices = next
      // Show it the moment it answers; the list settles when the scan ends.
      discovered = Model.upsertDevice(shown, device)
    } else if (event.event === "done") {
      discovered = _scanDevices
    }
  }

  // -------------------------------------------------------------- favorites

  function toggleFavorite(device) {
    if (!device) return
    persistFavorites(Model.toggleFavorite(favorites, device))
  }

  function persistFavorites(next) {
    if (!shell || typeof shell.updateEntryInline !== "function") return
    var entry = { id: moduleName }
    for (var key in settings) if (key !== "id") entry[key] = settings[key]
    entry.favorites = next
    shell.updateEntryInline(moduleName, entry)
  }

  // ---------------------------------------------------------------- sending

  function sendFiles(device) {
    if (!Model.isValidTarget(device) || !device.online || sending || pickingFiles) return
    _pickTarget = device
    pickingFiles = true
    lastError = ""
    pickProcess.command = ["omarchy-file-select", "--multiple", "--title", t("localsend.pickTitle").arg(device.alias)]
    pickProcess.running = true
  }

  function sendClipboard(device) {
    startSend(device, ["--clipboard"])
  }

  function sendPaths(device, paths) {
    // "--" ends the options, so a file whose name starts with "-" stays a file.
    startSend(device, ["--"].concat(paths))
  }

  function startSend(device, extra) {
    if (!Model.isValidTarget(device) || !device.online || sending) return
    sendTarget = device
    sending = true
    sendPhase = "waiting"
    progress = null
    lastError = ""
    actionStatus = ""
    _sendFinished = false
    // ip, port and protocol passed Model.isValidTarget and the fingerprint
    // Model.isValidFingerprint, so they are safe as arguments; nothing else
    // from the device record reaches the command.
    sendProcess.command = helperCommand.concat([
      "send", "--ip", String(device.ip), "--port", String(device.port), "--protocol", String(device.protocol)
    ]).concat(Model.fingerprintArgs(device)).concat(extra)
    sendProcess.running = true
  }

  function cancelSend() {
    if (sendProcess.running) sendProcess.signal(15)
  }

  function handleSendLine(line) {
    var event = Model.parseEvent(line)
    if (!event) return
    if (event.event === "waiting") sendPhase = "waiting"
    else if (event.event === "accepted") sendPhase = "accepted"
    else if (event.event === "progress") {
      sendPhase = "progress"
      progress = event
    } else if (event.event === "done") {
      _sendFinished = true
      finishSend(true, event)
    } else if (event.event === "error") {
      _sendFinished = true
      finishSend(false, event)
    }
  }

  function errorText(code) {
    var key = "localsend.err." + String(code || "generic")
    var text = strings ? strings.t(key, "") : ""
    return text !== "" ? text : t("localsend.err.generic")
  }

  function finishSend(ok, event) {
    var target = sendTarget ? String(sendTarget.alias) : ""
    if (ok) {
      var count = parseInt(String(event.files), 10) || 0
      var body = event.text === true
        ? t("localsend.sentText")
        : (strings ? strings.tn("localsend.sentFiles", count) : "%1 · %2")
            .arg(I18n.formatNumber(count)).arg(Model.formatBytes(event.totalSize, I18n.formatNumber))
      actionStatus = t("localsend.sentTo").arg(target)
      lastError = ""
      notify(actionStatus, body)
    } else {
      var code = String(event.code || "generic")
      lastError = errorText(code)
      if (code !== "cancelled") notify(t("localsend.failedTo").arg(target), lastError)
    }
    actionStatusTimer.restart()
  }

  function notify(headline, body) {
    Quickshell.execDetached(["omarchy-notification-send", "--app-name", "LocalSend", "-g", "󰒗", "-u", "normal", String(headline), String(body)])
  }

  onPanelOpenChanged: if (panelOpen) {
    refreshStatus()
    rescan()
  }

  // ------------------------------------------------------------------ timers

  Timer {
    id: statusTimer
    interval: 15000
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.refreshStatus()
  }

  Timer {
    id: scanTimer
    interval: root.scanIntervalSec * 1000
    repeat: true
    running: root.panelOpen
    onTriggered: root.rescan()
  }

  Timer {
    id: settleTimer
    interval: 1500
    repeat: false
    onTriggered: {
      root.refreshStatus()
      secondSettle.restart()
    }
  }

  Timer {
    id: secondSettle
    interval: 2500
    repeat: false
    onTriggered: root.refreshStatus()
  }

  Timer {
    // Reality never caught up: stop pretending and show what is.
    id: giveUpTimer
    interval: 10000
    repeat: false
    onTriggered: root._desiredReceiving = -1
  }

  Timer {
    id: actionStatusTimer
    interval: 4000
    repeat: false
    onTriggered: root.actionStatus = ""
  }

  Timer {
    id: discoverWatchdog
    interval: 15000
    repeat: false
    onTriggered: if (discoverProcess.running) discoverProcess.signal(15)
  }

  // --------------------------------------------------------------- processes

  Process {
    id: statusProcess
    running: false
    command: []
    stdout: StdioCollector { id: statusStdout; waitForEnd: true }
    onExited: function(exitCode) {
      if (exitCode === 0) root.applyStatus(statusStdout.text)
      else root.statusLoaded = true
    }
  }

  Process {
    id: discoverProcess
    running: false
    command: []
    stdout: SplitParser { onRead: function(data) { root.handleDiscoverLine(data) } }
    stderr: SplitParser { onRead: function(data) { console.warn("mae.localsend discover:", String(data).substring(0, 200)) } }
    onExited: function(exitCode) {
      root.scanning = false
      root.scannedOnce = true
      discoverWatchdog.stop()
      if (exitCode !== 0) root.discovered = root._scanDevices
    }
  }

  Process {
    id: sendProcess
    running: false
    command: []
    stdout: SplitParser { onRead: function(data) { root.handleSendLine(data) } }
    stderr: SplitParser { onRead: function(data) { console.warn("mae.localsend send:", String(data).substring(0, 200)) } }
    onExited: function(exitCode) {
      if (!root._sendFinished) root.finishSend(false, { code: exitCode === 143 || exitCode === 130 ? "cancelled" : "generic" })
      root.sending = false
      root.sendPhase = ""
      root.progress = null
    }
  }

  Process {
    id: pickProcess
    running: false
    command: []
    stdout: StdioCollector { id: pickStdout; waitForEnd: true }
    onExited: function(exitCode) {
      root.pickingFiles = false
      if (exitCode === 0) {
        var lines = String(pickStdout.text || "").split("\n")
        var paths = []
        for (var i = 0; i < lines.length; i++) if (lines[i].trim() !== "") paths.push(lines[i])
        if (paths.length > 0) root.sendPaths(root._pickTarget, paths)
      } else if (exitCode === 2) {
        root.lastError = root.t("localsend.chooserFailed")
      }
      root._pickTarget = null
    }
  }
}
