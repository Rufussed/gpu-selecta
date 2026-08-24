import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Panel {
  id: root

  moduleName: "rufussed.gpu-switch"
  ipcTarget: "rufussed.gpu-switch"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.45)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  property var gpus: []
  property bool isHybrid: false
  property string renderDefault: "amd"
  property var appGpuAssignments: ({})
  property string copiedNotice: ""
  property bool isUpdating: false

  property string newAppLabel: ""
  property string newAppDesktopFile: ""

  readonly property var appKeys: {
    var keys = Object.keys(root.appGpuAssignments)
    keys.sort(function(a, b) {
      return (root.appGpuAssignments[a].label || a).localeCompare(root.appGpuAssignments[b].label || b)
    })
    return keys
  }

  function scriptPath() {
    return Qt.resolvedUrl("scripts/gpu_switch_engine.py").toString().replace(/^file:\/\//, "")
  }

  function refresh() {
    if (pollProc.running) return
    root.isUpdating = true
    pollProc.running = true
  }

  function setRenderDefault(target) {
    controlProc.command = ["python3", root.scriptPath(), "--set-render-default", target]
    controlProc.running = true
  }

  function setAppGpu(appKey, gpu) {
    controlProc.command = ["python3", root.scriptPath(), "--set-app-gpu", appKey, gpu]
    controlProc.running = true
  }

  function removeApp(appKey) {
    controlProc.command = ["python3", root.scriptPath(), "--remove-app", appKey]
    controlProc.running = true
  }

  function addApp() {
    var label = root.newAppLabel.trim()
    var desktopFile = root.newAppDesktopFile.trim()
    if (!desktopFile) return
    if (!label) label = desktopFile.replace(/\.desktop$/, "")
    var appKey = desktopFile.replace(/\.desktop$/, "").toLowerCase().replace(/[^a-z0-9]+/g, "-")
    controlProc.command = ["python3", root.scriptPath(), "--add-app", appKey, label, desktopFile, "auto"]
    controlProc.running = true
    root.newAppLabel = ""
    root.newAppDesktopFile = ""
  }

  function parseOutput(text) {
    root.isUpdating = false
    if (!text || text.trim() === "") return
    try {
      var data = JSON.parse(text)
      root.gpus = data.gpus || []
      root.isHybrid = data.isHybrid === true
      root.renderDefault = data.renderDefault || "amd"
      root.appGpuAssignments = data.appGpuAssignments || ({})
    } catch (e) {
      console.log("gpu-switch JSON parse error:", e)
    }
  }

  Timer {
    id: noticeTimer
    interval: 2500
    running: false
    repeat: false
    onTriggered: root.copiedNotice = ""
  }

  Process {
    id: pollProc
    command: ["python3", root.scriptPath()]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.parseOutput(text)
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (text) console.log("gpu-switch stderr:", text)
    }
    onExited: function(c) { root.isUpdating = false }
  }

  Process {
    id: controlProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        if (text && text.trim() !== "") {
          try {
            var res = JSON.parse(text)
            if (res.status === "success") {
              if (res.renderDefault) root.copiedNotice = "Default renderer: " + (res.renderDefault === "nvidia" ? "NVIDIA" : "AMD")
              else if (res.appKey && res.gpu) root.copiedNotice = (res.label || res.appKey) + " -> " + res.gpu.toUpperCase()
              else if (res.appKey) root.copiedNotice = "Removed " + res.appKey
              noticeTimer.restart()
            } else if (res.status === "error") {
              root.copiedNotice = "Error: " + (res.message || "unknown")
              noticeTimer.restart()
            }
          } catch (e) {
            console.log("controlProc parse error:", e)
          }
        }
        root.refresh()
      }
    }
  }

  onOpenedChanged: {
    if (opened) {
      root.refresh()
      Qt.callLater(function() {
        if (keyCatcher) keyCatcher.forceActiveFocus()
      })
    }
  }

  Component.onCompleted: root.refresh()

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher

    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(mainLayout.implicitHeight, Style.space(620))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent

      onCloseRequested: root.close()
      onTextKey: function(t) {
        if (t === "r" || t === "R") root.refresh()
      }

      Column {
        id: mainLayout
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(10)

        // ------------------ HERO HEADER ------------------
        Item {
          width: parent.width
          implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight)

          Text {
            textFormat: Text.PlainText
            id: heroIcon
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: "󰢮"
            color: root.renderDefault === "nvidia" ? Color.accent : root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.display
          }

          Column {
            id: heroLabels
            anchors.left: heroIcon.right
            anchors.leftMargin: Style.space(12)
            anchors.right: heroAction.left
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(2)

            Text {
              textFormat: Text.PlainText
              text: "GPU Switch"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.title
              font.bold: true
            }

            Text {
              textFormat: Text.PlainText
              text: root.isHybrid ? "Hybrid GPU launch routing" : "No hybrid GPU setup detected"
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          PanelActionButton {
            id: heroAction
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            iconText: ""
            tooltipText: root.isUpdating ? "Refreshing..." : "Refresh ('R')"
            foreground: root.isUpdating ? Color.accent : root.foreground
            rotation: 0
            onClicked: root.refresh()

            RotationAnimation on rotation {
              from: 0
              to: 360
              duration: 800
              loops: Animation.Infinite
              running: root.isUpdating
            }
          }
        }

        // ------------------ COPIED NOTICE BANNER ------------------
        BorderSurface {
          visible: root.copiedNotice !== ""
          width: parent.width
          implicitHeight: noticeText.implicitHeight + Style.space(8)
          color: "transparent"
          borderSpec: Border.controlSpec("focus", Color.accent, Color.accent)
          radius: Style.cornerRadius

          Text {
            id: noticeText
            textFormat: Text.PlainText
            anchors.centerIn: parent
            text: root.copiedNotice
            color: Color.accent
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
            elide: Text.ElideMiddle
          }
        }

        PanelSeparator {
          width: parent.width
        }

        // ------------------ NOT A HYBRID SYSTEM NOTICE ------------------
        Text {
          visible: !root.isHybrid
          textFormat: Text.PlainText
          width: parent.width
          text: "This system doesn't have a hybrid AMD/NVIDIA (or Intel/NVIDIA) setup with more than one GPU, so there's nothing to route between."
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption
          wrapMode: Text.Wrap
        }

        // ------------------ GLOBAL DEFAULT RENDERER ------------------
        BorderSurface {
          visible: root.isHybrid
          width: parent.width
          implicitHeight: globalCol.implicitHeight + Style.space(16)
          color: Style.hoverFillFor(root.foreground, root.foreground)
          borderSpec: Border.controlSpec("normal", root.dim, Color.accent)
          radius: Style.cornerRadius

          Column {
            id: globalCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: Style.space(8)
            spacing: Style.space(6)

            Text {
              textFormat: Text.PlainText
              text: "Global Default (new apps)"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              font.bold: true
            }

            Row {
              spacing: Style.space(6)

              Repeater {
                model: [
                  { key: "amd", label: "AMD (default)" },
                  { key: "nvidia", label: "NVIDIA (offload)" }
                ]

                delegate: BorderSurface {
                  readonly property bool isActive: root.renderDefault === modelData.key
                  implicitWidth: globalChoiceText.implicitWidth + Style.space(14)
                  implicitHeight: globalChoiceText.implicitHeight + Style.space(8)
                  radius: Style.cornerRadius
                  color: isActive ? Style.selectedFillFor(root.foreground, root.foreground) : "transparent"
                  borderSpec: isActive
                    ? Border.controlSpec("selected", Color.accent, Color.accent)
                    : Border.controlSpec("normal", root.dim, Color.accent)

                  Text {
                    textFormat: Text.PlainText
                    id: globalChoiceText
                    anchors.centerIn: parent
                    text: modelData.label
                    color: isActive ? root.foreground : root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    font.bold: isActive
                  }

                  MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.setRenderDefault(modelData.key)
                  }
                }
              }
            }

            Text {
              textFormat: Text.PlainText
              width: parent.width
              text: "Not a hardware switch — applies to apps launched from now on. Already-running apps keep their current GPU."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.Wrap
            }
          }
        }

        // ------------------ PER-APP ASSIGNMENTS ------------------
        Column {
          visible: root.isHybrid
          width: parent.width
          spacing: Style.space(8)

          Text {
            textFormat: Text.PlainText
            text: "Per-App GPU"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body
            font.bold: true
          }

          Repeater {
            model: root.appKeys

            delegate: BorderSurface {
              readonly property string appKey: modelData
              readonly property var entry: root.appGpuAssignments[appKey] || ({})
              width: parent.width
              implicitHeight: appRow.implicitHeight + Style.space(12)
              radius: Style.cornerRadius
              color: Style.hoverFillFor(root.foreground, root.foreground)
              borderSpec: Border.controlSpec("normal", root.dim, Color.accent)

              Column {
                id: appRow
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: Style.space(8)
                spacing: Style.space(4)

                Row {
                  width: parent.width
                  spacing: Style.space(6)

                  Text {
                    textFormat: Text.PlainText
                    text: entry.label || appKey
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                    font.bold: true
                    width: parent.width - removeBtn.implicitWidth - Style.space(6)
                    elide: Text.ElideRight
                  }

                  PanelActionButton {
                    id: removeBtn
                    iconText: "󰅖"
                    tooltipText: "Reset / remove"
                    foreground: root.dim
                    onClicked: root.removeApp(appKey)
                  }
                }

                Row {
                  spacing: Style.space(6)

                  Repeater {
                    model: [
                      { key: "amd", label: "AMD" },
                      { key: "auto", label: "Auto" },
                      { key: "nvidia", label: "NVIDIA" }
                    ]

                    delegate: BorderSurface {
                      readonly property bool isActive: entry.gpu === modelData.key
                      implicitWidth: appChoiceText.implicitWidth + Style.space(12)
                      implicitHeight: appChoiceText.implicitHeight + Style.space(6)
                      radius: Style.cornerRadius
                      color: isActive ? Style.selectedFillFor(root.foreground, root.foreground) : "transparent"
                      borderSpec: isActive
                        ? Border.controlSpec("selected", Color.accent, Color.accent)
                        : Border.controlSpec("normal", root.dim, Color.accent)

                      Text {
                        textFormat: Text.PlainText
                        id: appChoiceText
                        anchors.centerIn: parent
                        text: modelData.label
                        color: isActive ? root.foreground : root.dim
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                        font.bold: isActive
                      }

                      MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.setAppGpu(appKey, modelData.key)
                      }
                    }
                  }
                }
              }
            }
          }
        }

        // ------------------ ADD APP ------------------
        BorderSurface {
          visible: root.isHybrid
          width: parent.width
          implicitHeight: addAppCol.implicitHeight + Style.space(16)
          color: "transparent"
          borderSpec: Border.controlSpec("normal", root.dim, root.dim)
          radius: Style.cornerRadius

          Column {
            id: addAppCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: Style.space(8)
            spacing: Style.space(6)

            Text {
              textFormat: Text.PlainText
              text: "Add App"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              font.bold: true
            }

            TextField {
              width: parent.width
              placeholderText: "Label (e.g. GIMP)"
              text: root.newAppLabel
              onTextChanged: root.newAppLabel = text
            }

            TextField {
              width: parent.width
              placeholderText: "Desktop file (e.g. gimp.desktop) — find via ls /usr/share/applications"
              text: root.newAppDesktopFile
              onTextChanged: root.newAppDesktopFile = text
            }

            PanelActionButton {
              iconText: "󰐕"
              tooltipText: "Add"
              foreground: Color.accent
              onClicked: root.addApp()
            }
          }
        }
      }
    }
  }
}
