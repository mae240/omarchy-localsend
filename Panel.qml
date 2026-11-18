import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// LocalSend in the bar: the icon shows whether this machine receives, the
// panel lists nearby devices with send actions, favorites, and the progress
// of the one transfer that can run at a time.
//
// Mouse: left = panel · right = receiver on/off · middle = scan again.
// Keys: j/k move · Enter send files · c clipboard · f favorite · x unfavorite
//       r scan again · t receiver on/off · o open LocalSend · Esc close.
Panel {
  id: root
  moduleName: "mae.localsend"
  ipcTarget: "mae.localsend"
  manageIpc: false

  readonly property Item panelFlick: panel.loadedContent ? panel.loadedContent.panelFlick : null
  readonly property Item deviceColumn: panel.loadedContent ? panel.loadedContent.deviceColumn : null

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color urgent: bar ? bar.urgent : Commons.Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color hoverFill: Style.hoverFillFor(foreground, Commons.Color.accent)
  readonly property color selectedFill: Style.selectedFillFor(foreground, Commons.Color.accent)

  property string focusSection: "header"
  property int deviceIndex: 0
  property bool cursorActive: false
  readonly property var devices: service.devices
  readonly property bool headerHasCursor: cursorActive && focusSection === "header"
  readonly property color barIconColor: service.receiverActive ? barForeground : Qt.darker(barForeground, 1.55)
  readonly property string barLabel: service.sending && service.progress && service.progress.totalSize > 0
    ? Commons.I18n.formatNumber(Math.round(Model.transferFraction(service.progress) * 100), 0) + "%"
    : ""
  readonly property string heroMeta: service.receiverBusy
    ? (service.receiverActive ? strings.t("localsend.receiverStarting") : strings.t("localsend.receiverStopping"))
    : (service.receiverActive ? strings.t("localsend.receivingOn").arg(service.receiverAddress) : strings.t("localsend.receiverOff"))
  readonly property string toggleHint: service.receiverActive ? strings.t("localsend.turnOff") : strings.t("localsend.turnOn")

  Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

  function selectedDevice() {
    if (devices.length === 0) return null
    return devices[Math.max(0, Math.min(deviceIndex, devices.length - 1))]
  }

  function ensureCursor() {
    if (deviceIndex >= devices.length) deviceIndex = Math.max(0, devices.length - 1)
    if (focusSection === "devices" && devices.length === 0) focusSection = "header"
  }

  function moveCursor(dx, dy) {
    cursorActive = true
    ensureCursor()
    if (dy > 0) {
      if (focusSection === "header") { if (devices.length > 0) focusSection = "devices" }
      else if (deviceIndex < devices.length - 1) deviceIndex++
    } else if (dy < 0) {
      if (focusSection === "devices") {
        if (deviceIndex <= 0) focusSection = "header"
        else deviceIndex--
      }
    }
    ensureCursor()
    scrollCursorIntoView()
  }

  function activateCursor() {
    ensureCursor()
    if (focusSection === "header") service.toggleReceiver()
    else service.sendFiles(selectedDevice())
  }

  function setHeaderCursor() {
    cursorActive = true
    focusSection = "header"
  }

  function setDeviceCursor(index) {
    cursorActive = true
    focusSection = "devices"
    deviceIndex = index
  }

  function scrollItemIntoView(item) {
    if (!panelFlick || !item) return
    Qt.callLater(function() {
      if (!item) return
      var margin = Style.space(6)
      var point = item.mapToItem(panelFlick.contentItem, 0, 0)
      var top = point.y
      var bottom = top + item.height
      var viewTop = panelFlick.contentY
      var viewBottom = viewTop + panelFlick.height
      var maxY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
      if (top < viewTop + margin) panelFlick.contentY = Math.max(0, top - margin)
      else if (bottom > viewBottom - margin) panelFlick.contentY = Math.min(maxY, bottom + margin - panelFlick.height)
    })
  }

  function scrollCursorIntoView() {
    if (focusSection === "devices" && deviceColumn && deviceIndex >= 0 && deviceIndex < deviceColumn.children.length)
      scrollItemIntoView(deviceColumn.children[deviceIndex])
    else if (panelFlick) panelFlick.contentY = 0
  }

  // The file picker takes over from here, so get the panel out of the way.
  function sendFilesTo(device) {
    if (!device || !device.online || service.sending) return
    service.sendFiles(device)
    close()
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onOpenedChanged: {
    service.panelOpen = opened
    if (opened) {
      cursorActive = false
      if (panelFlick) panelFlick.contentY = 0
      Qt.callLater(function() { if (panel.loadedContent) panel.loadedContent.forceActiveFocus() })
    }
  }
  onDevicesChanged: ensureCursor()
  onDeviceIndexChanged: scrollCursorIntoView()

  Service {
    id: service
    settings: root.settings
    moduleName: root.moduleName
    shell: root.bar ? root.bar.shell : null
    strings: strings
  }

  ShellIpc {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function rescan(): string { service.rescan(); return "ok" }
    function toggleReceiver(): string { service.toggleReceiver(); return "ok" }
    function status(): string { return service.receiverActive ? "receiving" : "off" }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰒗"
    labelText: root.barLabel
    foreground: root.barIconColor
    useActiveColor: false
    tooltipText: root.heroMeta
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) service.toggleReceiver()
      else if (buttonCode === Qt.MiddleButton) service.rescan()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: panel.loadedContent
    contentWidth: panel.fittedContentWidth(Style.space(380))
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight, Style.space(560))

    content: PanelKeyCatcher {
      id: keyCatcher
      property alias panelFlick: panelFlick
      property alias deviceColumn: deviceColumn
      implicitHeight: column.implicitHeight
      anchors.fill: parent
      onMoveRequested: function(dx, dy) {
        if (!root.cursorActive) { root.cursorActive = true; return }
        root.moveCursor(dx, dy)
      }
      onActivateRequested: if (root.cursorActive) root.activateCursor()
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onDeleteRequested: {
        var device = root.selectedDevice()
        if (root.focusSection === "devices" && device && device.favorite) service.toggleFavorite(device)
      }
      onTextKey: function(key) {
        var device = root.focusSection === "devices" ? root.selectedDevice() : null
        if (key === "r" || key === "R") service.rescan()
        else if (key === "t" || key === "T") service.toggleReceiver()
        else if (key === "o" || key === "O") service.openApp()
        else if (key === "c" || key === "C") service.sendClipboard(device)
        else if (key === "f" || key === "F") service.toggleFavorite(device)
        else if (key === "s" || key === "S") root.sendFilesTo(device)
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          Item {
            id: header
            width: parent.width
            implicitHeight: hero.implicitHeight
            readonly property bool ringVisible: root.headerHasCursor
            function focusHero() { root.setHeaderCursor() }

            PanelHero {
              id: hero
              width: parent.width
              title: service.alias !== "" ? service.alias : strings.t("localsend.title")
              meta: root.heroMeta
              detail: service.onlineCount > 0 ? strings.tn("localsend.devicesOnline", service.onlineCount).arg(Commons.I18n.formatNumber(service.onlineCount)) : ""
              foreground: root.foreground
              fontFamily: root.fontFamily
              iconOpacity: service.receiverActive ? 1.0 : 0.5
              iconComponent: Component {
                Text {
                  text: "󰒗"
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.display
                }
              }
              trailingControl: Component {
                ToggleSwitch {
                  id: receiverSwitch
                  checked: service.receiverActive
                  busy: service.receiverBusy
                  hasCursor: header.ringVisible
                  foreground: hero.foreground
                  onHovered: function(on) { if (on) header.focusHero() }
                  onToggled: service.toggleReceiver()

                  PanelToolTip {
                    visible: receiverSwitch.containsMouse
                    text: root.toggleHint
                    fontFamily: hero.fontFamily
                  }
                }
              }
            }
          }

          Text {
            textFormat: Text.PlainText
            visible: text !== ""
            width: parent.width
            text: service.actionStatus !== "" ? service.actionStatus : service.lastError
            color: service.lastError !== "" && service.actionStatus === "" ? root.urgent : root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
          }

          TransferCard {
            visible: service.sending
            width: parent.width
          }

          PanelSeparator { foreground: root.foreground }

          Column {
            width: parent.width
            spacing: Style.space(10)

            RowLayout {
              width: parent.width
              spacing: Style.space(8)

              PanelSectionHeader {
                Layout.fillWidth: true
                text: strings.t("localsend.nearby")
                foreground: root.foreground
                fontFamily: root.fontFamily
              }

              Text {
                visible: service.scanning
                textFormat: Text.PlainText
                text: strings.t("localsend.scanning")
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }

              PanelActionButton {
                iconText: "󰑐"
                tooltipText: strings.t("localsend.rescan")
                foreground: root.foreground
                fontFamily: root.fontFamily
                enabled: !service.scanning
                onClicked: service.rescan()
              }
            }

            Text {
              visible: root.devices.length === 0 && service.scannedOnce && !service.scanning
              width: parent.width
              text: strings.t("localsend.noDevices")
              textFormat: Text.PlainText
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
              horizontalAlignment: Text.AlignHCenter
            }

            Column {
              id: deviceColumn
              width: parent.width
              spacing: Style.space(6)

              Repeater {
                model: root.devices
                DeviceRow {
                  required property var modelData
                  required property int index
                  width: deviceColumn.width
                  device: modelData
                  rowIndex: index
                }
              }
            }
          }

          PanelSeparator { foreground: root.foreground }

          Row {
            width: parent.width
            spacing: Style.space(8)

            Button {
              iconText: "󰏌"
              text: strings.t("localsend.openApp")
              bordered: true
              foreground: root.foreground
              fontFamily: root.fontFamily
              enabled: service.appInstalled
              onClicked: {
                service.openApp()
                root.close()
              }
            }
          }
        }
      }
    }
  }

  // The running transfer: who, which file, how far, and a way out.
  component TransferCard: BorderSurface {
    id: card
    readonly property var target: service.sendTarget
    readonly property var progress: service.progress
    readonly property real fraction: Model.transferFraction(progress)
    readonly property string title: service.sendPhase === "waiting"
      ? strings.t("localsend.waitingFor").arg(target ? target.alias : "")
      : strings.t("localsend.sendingTo").arg(target ? target.alias : "")
    readonly property string detail: progress
      ? strings.t("localsend.fileOf").arg(String(progress.name || "")).arg(Commons.I18n.formatNumber((progress.index || 0) + 1)).arg(Commons.I18n.formatNumber(progress.count || 1))
      : ""
    readonly property string amount: progress
      ? Model.formatBytes(progress.totalSent, Commons.I18n.formatNumber) + " / " + Model.formatBytes(progress.totalSize, Commons.I18n.formatNumber)
      : ""

    implicitHeight: cardColumn.implicitHeight + Style.space(20)
    radius: Style.cornerRadius
    color: Style.normalFillFor(root.foreground, Commons.Color.accent)
    borderSpec: Border.controlSpec("normal", root.foreground, Commons.Color.accent)

    Column {
      id: cardColumn
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(12)
      anchors.rightMargin: Style.space(12)
      spacing: Style.space(6)

      RowLayout {
        width: parent.width
        spacing: Style.space(8)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: card.title
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: true
          elide: Text.ElideRight
        }

        PanelActionButton {
          iconText: "󰅖"
          tooltipText: strings.t("localsend.cancel")
          foreground: root.foreground
          hoverColor: root.urgent
          fontFamily: root.fontFamily
          onClicked: service.cancelSend()
        }
      }

      Text {
        textFormat: Text.PlainText
        visible: text !== ""
        width: parent.width
        text: card.detail
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideMiddle
      }

      Rectangle {
        width: parent.width
        height: Style.space(4)
        radius: height / 2
        color: Style.selectedFillFor(root.foreground, Commons.Color.accent)

        Rectangle {
          width: parent.width * (service.sendPhase === "waiting" ? 0 : card.fraction)
          height: parent.height
          radius: height / 2
          color: root.foreground
          Behavior on width { NumberAnimation { duration: Style.duration(120); easing.type: Easing.OutCubic } }
        }
      }

      Text {
        textFormat: Text.PlainText
        visible: text !== ""
        width: parent.width
        text: card.amount
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }
    }
  }

  component DeviceRow: CursorSurface {
    id: row
    property var device: null
    property int rowIndex: 0
    readonly property bool online: device && device.online === true
    readonly property bool favorite: device && device.favorite === true
    readonly property bool canSend: online && !service.sending && !service.pickingFiles
    readonly property string subtitle: device ? (online ? Model.deviceSubtitle(device) : strings.t("localsend.offline")) : ""

    hasCursor: root.cursorActive && root.focusSection === "devices" && root.deviceIndex === rowIndex
    foreground: root.foreground
    fill: root.hoverFill
    currentFill: root.selectedFill
    opacity: online ? 1.0 : 0.6

    implicitHeight: Math.max(rowContent.implicitHeight, sendButton.implicitHeight) + Style.spacing.rowPaddingX

    MouseArea {
      anchors.fill: parent
      acceptedButtons: Qt.LeftButton
      hoverEnabled: true
      cursorShape: Qt.ArrowCursor
      onContainsMouseChanged: if (containsMouse) root.setDeviceCursor(row.rowIndex)
    }

    RowLayout {
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.space(10)
      anchors.rightMargin: Style.space(6)
      spacing: Style.space(8)

      Text {
        textFormat: Text.PlainText
        text: Model.deviceIcon(row.device ? row.device.deviceType : "")
        color: row.online ? root.foreground : root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.icon
        Layout.alignment: Qt.AlignVCenter
      }

      ColumnLayout {
        id: rowContent
        Layout.fillWidth: true
        spacing: Style.space(1)

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: row.device ? String(row.device.alias) : ""
          color: root.foreground
          font.family: root.fontFamily
          font.pixelSize: Style.font.body
          font.bold: row.favorite
          elide: Text.ElideRight
        }

        Text {
          textFormat: Text.PlainText
          Layout.fillWidth: true
          text: row.subtitle
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
        }
      }

      PanelActionButton {
        iconText: row.favorite ? "󰓎" : "󰓒"
        tooltipText: row.favorite ? strings.t("localsend.removeFavorite") : strings.t("localsend.addFavorite")
        foreground: root.foreground
        fontFamily: root.fontFamily
        Layout.alignment: Qt.AlignVCenter
        onClicked: service.toggleFavorite(row.device)
      }

      PanelActionButton {
        iconText: "󰅐"
        tooltipText: strings.t("localsend.sendClipboard")
        foreground: root.foreground
        fontFamily: root.fontFamily
        enabled: row.canSend
        Layout.alignment: Qt.AlignVCenter
        onClicked: service.sendClipboard(row.device)
      }

      PanelActionButton {
        id: sendButton
        iconText: "󰒊"
        tooltipText: strings.t("localsend.sendFiles")
        foreground: root.foreground
        fontFamily: root.fontFamily
        enabled: row.canSend
        Layout.alignment: Qt.AlignVCenter
        onClicked: root.sendFilesTo(row.device)
      }
    }
  }
}
