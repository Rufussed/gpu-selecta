import QtQuick
import QtQuick.Controls
import QtQuick.Effects
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
  property string copiedNotice: ""
  property bool isUpdating: false
  property string activeTab: "overview"

  property var apps: ({})
  property bool appsLoaded: false
  property bool appsLoading: false
  property string appFilter: ""
  property string expandedGpuId: ""

  readonly property var tabList: [
    { label: "Overview", key: "overview" },
    { label: "Apps", key: "apps" }
  ]

  // "amd" is really "the non-NVIDIA GPU" in the render-default toggle — name
  // it after whichever vendor is actually integrated on this machine (AMD or
  // Intel) rather than assuming AMD.
  readonly property var routingChoices: {
    var choices = []
    for (var i = 0; i < root.gpus.length; i++) {
      var gpu = root.gpus[i]
      if (gpu.routeKey) {
        choices.push({
          key: gpu.routeKey,
          label: gpu.shortName || gpu.displayName || ("GPU " + (i + 1)),
          fullLabel: gpu.displayName || ("GPU " + (i + 1)),
          role: gpu.role || ""
        })
      }
    }
    choices.sort(function(a, b) {
      var rank = { "Integrated": 0, "Discrete": 1 }
      return (rank[a.role] !== undefined ? rank[a.role] : 2) -
             (rank[b.role] !== undefined ? rank[b.role] : 2)
    })
    return choices
  }

  readonly property var appRoutingChoices: {
    var choices = [{ key: "auto", label: "Default", role: "" }]
    for (var i = 0; i < root.routingChoices.length; i++) {
      choices.push({
        key: root.routingChoices[i].key,
        label: root.routingChoices[i].label,
        role: ""
      })
    }
    return choices
  }

  function routingLabel(key) {
    for (var i = 0; i < root.routingChoices.length; i++) {
      if (root.routingChoices[i].key === key) return root.routingChoices[i].fullLabel
    }
    return key
  }

  readonly property var filteredAppKeys: {
    var keys = Object.keys(root.apps)
    var f = root.appFilter.trim().toLowerCase()
    if (f !== "") {
      keys = keys.filter(function(k) {
        return (root.apps[k].label || k).toLowerCase().indexOf(f) !== -1
      })
    }
    keys.sort(function(a, b) {
      return (root.apps[a].label || a).localeCompare(root.apps[b].label || b)
    })
    return keys
  }

  function scriptPath() {
    return Qt.resolvedUrl("scripts/gpu_switch_engine.py").toString().replace(/^file:\/\//, "")
  }

  function escapeHtml(text) {
    return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  }

  readonly property var clickSoundPaths: [
    Qt.resolvedUrl("assets/scratch1.mp3").toString().replace(/^file:\/\//, ""),
    Qt.resolvedUrl("assets/scratch2.mp3").toString().replace(/^file:\/\//, ""),
    Qt.resolvedUrl("assets/scratch3.mp3").toString().replace(/^file:\/\//, "")
  ]
  property int clickSoundIndex: 0

  function shellQuote(str) {
    return "'" + String(str).replace(/'/g, "'\\''") + "'"
  }

  property bool soundMuted: false

  function playButtonSound() {
    if (!root.bar) return
    var path = root.clickSoundPaths[root.clickSoundIndex]
    root.clickSoundIndex = (root.clickSoundIndex + 1) % root.clickSoundPaths.length
    if (!root.soundMuted) {
      root.bar.run("mpv --no-video --really-quiet " + root.shellQuote(path))
    }
    root.playScratchGif()
  }

  // Ping-pong: forward click plays the scratch gif start→end via
  // AnimatedImage's native playback; reverse click steps currentFrame
  // end→start by hand on a Timer, since QMovie/AnimatedImage has no native
  // reverse-playback mode. Outside of those two, "idle" loops one of two
  // calmer gifs: scratch-idle before the first click and after every reverse
  // (2nd, 4th, ... press) finishes; scratch-idle2 after every forward
  // (1st, 3rd, ... press) finishes.
  readonly property url scratchGifSourceIdle: Qt.resolvedUrl("assets/scratch-idle.gif")
  readonly property url scratchGifSourceIdle2: Qt.resolvedUrl("assets/scratch-idle2.gif")
  readonly property url scratchGifSourceActive: Qt.resolvedUrl("assets/scratch.gif")
  property string scratchGifMode: "idle" // "idle" | "forward" | "reverse"
  property int scratchGifIdleVariant: 1 // 1 = scratch-idle, 2 = scratch-idle2
  property bool scratchGifForwardNext: true

  function playScratchGif() {
    if (root.scratchGifMode !== "idle") return
    root.scratchGifMode = root.scratchGifForwardNext ? "forward" : "reverse"
    root.scratchGifForwardNext = !root.scratchGifForwardNext
  }

  // GPU model strings look like "GA106M [GeForce RTX 3060 Mobile / Max-Q] (rev a1)"
  // — the bit between "[" and "/" is the actual marketing name and the part
  // worth drawing the eye to; the chip codename and revision are noise.
  function formatModelText(model) {
    if (!model) return ""
    var start = model.indexOf("[")
    var slash = start >= 0 ? model.indexOf("/", start) : -1
    if (start === -1 || slash === -1) return root.escapeHtml(model)
    var before = model.substring(0, start + 1)
    var highlight = model.substring(start + 1, slash)
    var after = model.substring(slash)
    return root.escapeHtml(before) + "<b><font color=\"" + Color.accent + "\">" +
      root.escapeHtml(highlight) + "</font></b>" + root.escapeHtml(after)
  }

  function refresh() {
    if (pollProc.running) return
    root.isUpdating = true
    pollProc.running = true
  }

  function loadApps() {
    if (listAppsProc.running) return
    root.appsLoading = true
    listAppsProc.running = true
  }

  function setRenderDefault(target) {
    controlProc.command = ["python3", root.scriptPath(), "--set-render-default", target]
    controlProc.running = true
  }

  function setAppGpu(appKey, gpu) {
    var entry = root.apps[appKey] || {}
    var label = entry.label || appKey
    var files = (entry.desktopFiles || []).join(",")
    controlProc.command = ["python3", root.scriptPath(), "--set-app-gpu", appKey, gpu, label, files]
    controlProc.running = true
  }

  function toggleGpuManage(gpuId) {
    root.expandedGpuId = (root.expandedGpuId === gpuId) ? "" : gpuId
  }

  function setPowerProfile(gpuId, level) {
    controlProc.command = ["python3", root.scriptPath(), "--set-power-profile", gpuId, level]
    controlProc.running = true
  }

  function setFanPwm(gpuId, pwmVal) {
    controlProc.command = ["python3", root.scriptPath(), "--set-fan", gpuId, pwmVal.toString()]
    controlProc.running = true
  }

  function parseOutput(text) {
    root.isUpdating = false
    if (!text || text.trim() === "") return
    try {
      var data = JSON.parse(text)
      root.gpus = data.gpus || []
      root.isHybrid = data.isHybrid === true
      root.renderDefault = data.renderDefault || "amd"
    } catch (e) {
      console.log("gpu-switch JSON parse error:", e)
    }
  }

  function parseAppsOutput(text) {
    root.appsLoading = false
    root.appsLoaded = true
    if (!text || text.trim() === "") return
    try {
      var data = JSON.parse(text)
      root.apps = data.apps || ({})
    } catch (e) {
      console.log("gpu-switch apps parse error:", e)
    }
  }

  Timer {
    id: liveTimer
    interval: 2500
    running: root.opened
    repeat: true
    onTriggered: root.refresh()
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
    id: listAppsProc
    command: ["python3", root.scriptPath(), "--list-apps"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.parseAppsOutput(text)
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (text) console.log("gpu-switch stderr:", text)
    }
    onExited: function(c) { root.appsLoading = false }
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
              if (res.renderDefault) {
                root.copiedNotice = "Default renderer: " + root.routingLabel(res.renderDefault)
              } else if (res.appKey && res.gpu) {
                // The pill highlight already shows the new selection — no
                // need for a redundant top-of-panel banner on every click.
                // Clear any stale notice so an older one doesn't linger.
                root.copiedNotice = ""
                if (root.apps[res.appKey]) {
                  // Mutating root.apps[key] in place wouldn't notify QML's
                  // bindings — reassign the whole object so delegates re-evaluate.
                  var updatedApps = Object.assign({}, root.apps)
                  updatedApps[res.appKey] = Object.assign({}, updatedApps[res.appKey], { gpu: res.gpu })
                  root.apps = updatedApps
                }
              } else if (res.level) {
                root.copiedNotice = "Power governor: " + res.level
              } else if (res.mode === "auto") {
                root.copiedNotice = "Fan: Automatic"
              } else if (res.pwm !== undefined) {
                root.copiedNotice = "Fan PWM: " + res.pwm
              }
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
      if (!root.appsLoaded) root.loadApps()
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

    contentWidth: panel.fittedContentWidth(Style.space(440))
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

        // ------------------ HERO HEADER + SCRATCH GIF ------------------
        // Gif pinned top-right, shown at its native 200x133 size.
        Item {
          width: parent.width
          implicitHeight: Math.max(scratchGifImage.height, headerLeft.implicitHeight + Style.space(10) + tabsRow.implicitHeight)

          AnimatedImage {
            id: scratchGifImage
            anchors.top: parent.top
            anchors.right: parent.right
            width: 200
            height: 133
            fillMode: Image.PreserveAspectFit
            source: {
              if (root.scratchGifMode === "forward" || root.scratchGifMode === "reverse") return root.scratchGifSourceActive
              return root.scratchGifIdleVariant === 2 ? root.scratchGifSourceIdle2 : root.scratchGifSourceIdle
            }
            playing: root.scratchGifMode === "idle" || root.scratchGifMode === "forward"
            speed: root.scratchGifMode === "forward" ? 2.0 : 0.5
            cache: true

            onStatusChanged: {
              if (status !== Image.Ready) return
              if (root.scratchGifMode === "forward") {
                currentFrame = 0
              } else if (root.scratchGifMode === "reverse") {
                currentFrame = frameCount - 1
                scratchGifReverseTimer.start()
              }
            }

            onCurrentFrameChanged: {
              if (root.scratchGifMode !== "forward" || !playing) return
              if (currentFrame >= frameCount - 1) {
                root.scratchGifIdleVariant = 2
                root.scratchGifMode = "idle"
              }
            }
          }

          // Manual reverse stepper for the scratch gif. Its native per-frame
          // delay is 80ms; halved to 40ms to match the forward pass's 2x speed.
          Timer {
            id: scratchGifReverseTimer
            interval: 40
            repeat: true
            running: false
            onTriggered: {
              if (root.scratchGifMode !== "reverse") {
                scratchGifReverseTimer.stop()
                return
              }
              if (scratchGifImage.currentFrame <= 0) {
                scratchGifReverseTimer.stop()
                root.scratchGifIdleVariant = 1
                root.scratchGifMode = "idle"
                return
              }
              scratchGifImage.currentFrame -= 1
            }
          }

          Item {
            id: headerLeft
            anchors.top: parent.top
            anchors.left: parent.left
            anchors.right: scratchGifImage.left
            anchors.rightMargin: Style.space(12)
            implicitHeight: heroTopRow.implicitHeight + Style.space(8) + heroButtonRow.implicitHeight

            Item {
              id: heroTopRow
              anchors.top: parent.top
              anchors.left: parent.left
              anchors.right: parent.right
              implicitHeight: Math.max(heroIcon.implicitHeight, heroLabels.implicitHeight)

              Item {
                id: heroIcon
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                implicitWidth: 32
                implicitHeight: 24

                Image {
                  id: heroIconImage
                  anchors.fill: parent
                  source: Qt.resolvedUrl("assets/gpu-selecta-icon.svg")
                  sourceSize.width: Math.round(width * Screen.devicePixelRatio)
                  sourceSize.height: Math.round(height * Screen.devicePixelRatio)
                  fillMode: Image.PreserveAspectFit
                  visible: false
                  layer.enabled: true
                }

                MultiEffect {
                  anchors.fill: heroIconImage
                  source: heroIconImage
                  colorization: 1.0
                  colorizationColor: root.renderDefault === "nvidia" ? Color.accent : root.foreground
                }
              }

              Column {
                id: heroLabels
                anchors.left: heroIcon.right
                anchors.leftMargin: Style.space(12)
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(2)

                Text {
                  textFormat: Text.PlainText
                  text: "GPU Selecta"
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.title
                  font.bold: true
                }

                Text {
                  textFormat: Text.PlainText
                  text: "Spin your GPUs your way!"
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
            }

            Row {
              id: heroButtonRow
              anchors.top: heroTopRow.bottom
              anchors.topMargin: Style.space(8)
              anchors.right: parent.right
              spacing: Style.space(4)

              PanelActionButton {
                id: heroMuteAction
                iconText: root.soundMuted ? "" : ""
                tooltipText: root.soundMuted ? "Unmute button sounds" : "Mute button sounds"
                foreground: root.soundMuted ? root.dim : root.foreground
                onClicked: root.soundMuted = !root.soundMuted
              }
            }
          }

          // ------------------ NAVIGATION TABS ------------------
          // Flows below the logo/title/mute block, left of the gif, so the
          // tabs share the header's vertical space instead of adding a row.
          Row {
            id: tabsRow
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            anchors.right: scratchGifImage.left
            anchors.rightMargin: Style.space(12)
            spacing: Style.space(6)

            Repeater {
              model: root.tabList
              delegate: BorderSurface {
                readonly property bool isSelected: root.activeTab === modelData.key
                implicitWidth: tabText.implicitWidth + Style.space(14)
                implicitHeight: tabText.implicitHeight + Style.space(8)
                radius: Style.cornerRadius
                color: isSelected ? Style.selectedFillFor(root.foreground, root.foreground) : "transparent"
                borderSpec: isSelected
                  ? Border.controlSpec("selected", Color.accent, Color.accent)
                  : Border.controlSpec("normal", root.dim, Color.accent)

                Text {
                  textFormat: Text.PlainText
                  id: tabText
                  anchors.centerIn: parent
                  text: modelData.label
                  color: isSelected ? root.foreground : root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: isSelected
                }

                MouseArea {
                  anchors.fill: parent
                  cursorShape: Qt.PointingHandCursor
                  onClicked: {
                    root.activeTab = modelData.key
                    if (modelData.key === "apps" && !root.appsLoaded) root.loadApps()
                  }
                }
              }
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

        // =========================================================================
        // TAB: OVERVIEW
        // =========================================================================
        Column {
          visible: root.activeTab === "overview"
          width: parent.width
          spacing: Style.space(10)

          Text {
            visible: !root.isHybrid
            textFormat: Text.PlainText
            width: parent.width
            text: "No additional GPU can be selected safely with the installed drivers. Detected GPUs remain available below for telemetry."
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
                text: "Select Global Default"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
              }

              Row {
                spacing: Style.space(6)

                Repeater {
                  model: root.routingChoices

                  delegate: BorderSurface {
                    readonly property bool isActive: root.renderDefault === modelData.key
                    implicitWidth: globalChoiceLabels.implicitWidth + Style.space(18)
                    implicitHeight: globalChoiceLabels.implicitHeight + Style.space(10)
                    radius: Style.cornerRadius
                    color: isActive ? Style.selectedFillFor(root.foreground, root.foreground) : "transparent"
                    borderSpec: isActive
                      ? Border.controlSpec("selected", Color.accent, Color.accent)
                      : Border.controlSpec("normal", root.dim, Color.accent)

                    Column {
                      id: globalChoiceLabels
                      anchors.centerIn: parent
                      spacing: Style.space(1)

                      Text {
                        anchors.horizontalCenter: parent.horizontalCenter
                        textFormat: Text.PlainText
                        text: modelData.label
                        color: isActive ? root.foreground : root.dim
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                        font.bold: isActive
                      }

                      Text {
                        visible: Boolean(modelData.role)
                        anchors.horizontalCenter: parent.horizontalCenter
                        textFormat: Text.PlainText
                        text: modelData.role
                        color: isActive ? Color.accent : root.dim
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                      }
                    }

                    MouseArea {
                      anchors.fill: parent
                      cursorShape: Qt.PointingHandCursor
                      onClicked: {
                        root.playButtonSound()
                        root.setRenderDefault(modelData.key)
                      }
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

          // ------------------ LIVE TELEMETRY (both GPUs) ------------------
          Repeater {
            model: root.gpus

            delegate: BorderSurface {
              readonly property var gpu: modelData
              readonly property bool expanded: root.expandedGpuId === gpu.id
              width: parent.width
              implicitHeight: gpuCardCol.implicitHeight + Style.space(14)
              color: Style.hoverFillFor(root.foreground, root.foreground)
              borderSpec: Border.controlSpec("normal", root.dim, Color.accent)
              radius: Style.cornerRadius

              Column {
                id: gpuCardCol
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: Style.space(8)
                spacing: Style.space(4)

                Item {
                  width: parent.width
                  implicitHeight: Math.max(headerRow.implicitHeight, manageBtn.implicitHeight)

                  Column {
                    id: headerRow
                    anchors.left: parent.left
                    anchors.right: manageBtn.left
                    anchors.rightMargin: Style.space(6)
                    spacing: Style.space(2)

                    Text {
                      id: gpuNameText
                      textFormat: Text.PlainText
                      text: gpu.displayName || ("GPU " + (index + 1))
                      color: gpu.vendor === "NVIDIA" ? Color.accent : root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                      font.bold: true
                      width: parent.width
                      elide: Text.ElideRight
                    }

                    Text {
                      textFormat: Text.PlainText
                      text: (gpu.role ? gpu.role + " · " : "") + (gpu.driver || "unknown")
                      color: root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      elide: Text.ElideRight
                      width: parent.width
                    }
                  }

                  BorderSurface {
                    id: manageBtn
                    anchors.right: parent.right
                    anchors.top: parent.top
                    implicitWidth: manageRow.implicitWidth + Style.space(14)
                    implicitHeight: manageRow.implicitHeight + Style.space(6)
                    radius: Style.cornerRadius
                    color: expanded ? Style.selectedFillFor(root.foreground, root.foreground) : "transparent"
                    borderSpec: expanded
                      ? Border.controlSpec("selected", Color.accent, Color.accent)
                      : Border.controlSpec("normal", root.dim, Color.accent)

                    Row {
                      id: manageRow
                      anchors.centerIn: parent
                      spacing: Style.space(4)

                      Text {
                        textFormat: Text.PlainText
                        text: ""
                        color: expanded ? Color.accent : root.dim
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                      }

                      Text {
                        textFormat: Text.PlainText
                        text: "Manage"
                        color: expanded ? root.foreground : root.dim
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                        font.bold: expanded
                      }
                    }

                    MouseArea {
                      anchors.fill: parent
                      cursorShape: Qt.PointingHandCursor
                      onClicked: root.toggleGpuManage(gpu.id)
                    }
                  }
                }

                Row {
                  spacing: Style.space(14)

                  Text {
                    textFormat: Text.PlainText
                    text: (gpu.tempC !== null && gpu.tempC !== undefined) ? Math.round(gpu.tempC) + "°C" : "— °C"
                    color: (gpu.tempC !== null && gpu.tempC >= 80) ? root.urgent : root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }

                  Text {
                    textFormat: Text.PlainText
                    text: (gpu.busyPercent !== null && gpu.busyPercent !== undefined) ? gpu.busyPercent + "% busy" : "— busy"
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }

                  Text {
                    textFormat: Text.PlainText
                    text: (gpu.powerWatts !== null && gpu.powerWatts !== undefined) ? gpu.powerWatts + " W" : "— W"
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }

                Rectangle {
                  id: vramBarTrack
                  visible: gpu.vramTotalMb !== null && gpu.vramTotalMb !== undefined
                  width: parent.width
                  height: Style.space(6)
                  radius: height / 2
                  color: Qt.darker(root.foreground, 4)

                  Rectangle {
                    width: vramBarTrack.width * Math.max(0, Math.min(1, (gpu.vramPercent || 0) / 100))
                    height: parent.height
                    radius: height / 2
                    color: (gpu.vramPercent || 0) >= 90 ? root.urgent : ((gpu.vramPercent || 0) >= 70 ? Color.accent : "#87c095")
                  }
                }

                Text {
                  textFormat: Text.PlainText
                  visible: gpu.vramTotalMb !== null && gpu.vramTotalMb !== undefined
                  text: Math.round(gpu.vramUsedMb || 0) + " / " + Math.round(gpu.vramTotalMb || 0) + " MB VRAM" +
                        ((gpu.vramPercent !== null && gpu.vramPercent !== undefined) ? " (" + gpu.vramPercent + "%)" : "")
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }

                // ------------------ MANAGE (power governor + fan) ------------------
                Column {
                  visible: expanded
                  width: parent.width
                  spacing: Style.space(8)

                  PanelSeparator { width: parent.width }

                  Column {
                    width: parent.width
                    spacing: Style.space(4)

                    Text {
                      textFormat: Text.PlainText
                      text: "Power Governor"
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      font.bold: true
                    }

                    Text {
                      textFormat: Text.PlainText
                      visible: !gpu.supportsTuning
                      width: parent.width
                      text: "Not supported: DPM governors are amdgpu-only (driver: " + (gpu.driver || "unknown") + ")"
                      color: root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      wrapMode: Text.Wrap
                    }

                    Row {
                      visible: gpu.supportsTuning
                      spacing: Style.space(6)

                      Repeater {
                        model: [
                          { key: "auto", label: "Auto" },
                          { key: "high", label: "High" },
                          { key: "low", label: "Low" },
                          { key: "profile_peak", label: "Peak" }
                        ]

                        delegate: BorderSurface {
                          readonly property bool isActive: gpu.performanceLevel === modelData.key
                          implicitWidth: govText.implicitWidth + Style.space(12)
                          implicitHeight: govText.implicitHeight + Style.space(6)
                          radius: Style.cornerRadius
                          color: isActive ? Style.selectedFillFor(root.foreground, root.foreground) : "transparent"
                          borderSpec: isActive
                            ? Border.controlSpec("selected", Color.accent, Color.accent)
                            : Border.controlSpec("normal", root.dim, Color.accent)

                          Text {
                            textFormat: Text.PlainText
                            id: govText
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
                            onClicked: root.setPowerProfile(gpu.id, modelData.key)
                          }
                        }
                      }
                    }
                  }

                  Column {
                    width: parent.width
                    spacing: Style.space(4)

                    Text {
                      textFormat: Text.PlainText
                      text: "Fan"
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      font.bold: true
                    }

                    Text {
                      textFormat: Text.PlainText
                      visible: !gpu.supportsFanControl
                      width: parent.width
                      text: gpu.vendor === "NVIDIA"
                        ? "Not supported: NVIDIA laptop GPUs have no fan PWM node (EC-controlled)."
                        : "Not supported: no fan PWM node exposed (driver: " + (gpu.driver || "unknown") + ")"
                      color: root.dim
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.caption
                      wrapMode: Text.Wrap
                    }

                    Flow {
                      visible: gpu.supportsFanControl
                      width: parent.width
                      spacing: Style.space(6)

                      Repeater {
                        model: [
                          { key: "auto", label: "Auto", pwm: "auto" },
                          { key: "35", label: "35%", pwm: "90" },
                          { key: "60", label: "60%", pwm: "153" },
                          { key: "80", label: "80%", pwm: "204" },
                          { key: "100", label: "100%", pwm: "255" }
                        ]

                        delegate: BorderSurface {
                          readonly property bool isActive: modelData.key === "auto"
                            ? gpu.fanMode === "auto"
                            : (gpu.fanMode === "manual" && modelData.key !== "auto")
                          implicitWidth: fanText.implicitWidth + Style.space(12)
                          implicitHeight: fanText.implicitHeight + Style.space(6)
                          radius: Style.cornerRadius
                          color: isActive ? Style.selectedFillFor(root.foreground, root.foreground) : "transparent"
                          borderSpec: isActive
                            ? Border.controlSpec("selected", Color.accent, Color.accent)
                            : Border.controlSpec("normal", root.dim, Color.accent)

                          Text {
                            textFormat: Text.PlainText
                            id: fanText
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
                            onClicked: root.setFanPwm(gpu.id, modelData.pwm)
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }

        // =========================================================================
        // TAB: APPS
        // =========================================================================
        Column {
          visible: root.activeTab === "apps"
          width: parent.width
          spacing: Style.space(8)

          Row {
            width: parent.width
            spacing: Style.space(6)

            TextField {
              width: parent.width - refreshAppsBtn.implicitWidth - Style.space(6)
              placeholderText: "Filter apps..."
              text: root.appFilter
              onTextChanged: root.appFilter = text
            }

            PanelActionButton {
              id: refreshAppsBtn
              iconText: ""
              tooltipText: root.appsLoading ? "Scanning installed apps..." : "Rescan installed apps"
              foreground: root.appsLoading ? Color.accent : root.foreground
              onClicked: root.loadApps()

              RotationAnimation on rotation {
                from: 0
                to: 360
                duration: 800
                loops: Animation.Infinite
                running: root.appsLoading
              }
            }
          }

          Text {
            visible: root.appsLoading && Object.keys(root.apps).length === 0
            textFormat: Text.PlainText
            text: "Scanning installed applications..."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Text {
            visible: !root.appsLoading && root.filteredAppKeys.length === 0
            textFormat: Text.PlainText
            text: Object.keys(root.apps).length === 0 ? "No apps found." : "No apps match your filter."
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          Item {
            width: parent.width
            height: Math.min(appsColumn.implicitHeight, Style.space(380))

            Flickable {
              id: appsFlick
              anchors.left: parent.left
              anchors.right: appsScrollTrack.left
              anchors.rightMargin: Style.space(6)
              anchors.top: parent.top
              anchors.bottom: parent.bottom
              contentWidth: width
              contentHeight: appsColumn.implicitHeight
              clip: true
              boundsBehavior: Flickable.StopAtBounds

              Column {
                id: appsColumn
                width: appsFlick.width
                spacing: Style.space(6)

                Repeater {
                  model: root.filteredAppKeys

                  delegate: BorderSurface {
                    readonly property string appKey: modelData
                    readonly property var entry: root.apps[appKey] || ({})
                    width: appsColumn.width
                    implicitHeight: appRowCol.implicitHeight + Style.space(10)
                    radius: Style.cornerRadius
                    color: Style.hoverFillFor(root.foreground, root.foreground)
                    borderSpec: Border.controlSpec("normal", root.dim, Color.accent)

                    Column {
                      id: appRowCol
                      anchors.left: parent.left
                      anchors.right: parent.right
                      anchors.top: parent.top
                      anchors.margins: Style.space(8)
                      spacing: Style.space(4)

                      Text {
                        textFormat: Text.PlainText
                        text: entry.label || appKey
                        color: root.foreground
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.body
                        font.bold: true
                        width: parent.width
                        elide: Text.ElideRight
                      }

                      Row {
                        spacing: Style.space(6)

                        Repeater {
                          model: root.appRoutingChoices

                          delegate: BorderSurface {
                            readonly property bool isActive: entry.gpu === modelData.key
                            implicitWidth: appChoiceLabels.implicitWidth + Style.space(14)
                            implicitHeight: appChoiceLabels.implicitHeight + Style.space(8)
                            radius: Style.cornerRadius
                            color: isActive ? Style.selectedFillFor(root.foreground, root.foreground) : "transparent"
                            borderSpec: isActive
                              ? Border.controlSpec("selected", Color.accent, Color.accent)
                              : Border.controlSpec("normal", root.dim, Color.accent)

                            Column {
                              id: appChoiceLabels
                              anchors.centerIn: parent
                              spacing: Style.space(1)

                              Text {
                                anchors.horizontalCenter: parent.horizontalCenter
                                textFormat: Text.PlainText
                                text: modelData.label
                                color: isActive ? root.foreground : root.dim
                                font.family: root.fontFamily
                                font.pixelSize: Style.font.caption
                                font.bold: isActive
                              }

                              Text {
                                visible: Boolean(modelData.role)
                                anchors.horizontalCenter: parent.horizontalCenter
                                textFormat: Text.PlainText
                                text: modelData.role
                                color: isActive ? Color.accent : root.dim
                                font.family: root.fontFamily
                                font.pixelSize: Style.font.caption
                              }
                            }

                            MouseArea {
                              anchors.fill: parent
                              cursorShape: Qt.PointingHandCursor
                              onClicked: {
                                root.playButtonSound()
                                root.setAppGpu(appKey, modelData.key)
                              }
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }

            Rectangle {
              id: appsScrollTrack
              visible: appsFlick.contentHeight > appsFlick.height
              anchors.right: parent.right
              anchors.top: parent.top
              anchors.bottom: parent.bottom
              width: Style.space(10)
              radius: width / 2
              color: Qt.darker(root.foreground, 4)

              Rectangle {
                id: appsScrollThumb
                width: parent.width
                radius: width / 2
                color: scrollDragArea.pressed ? root.foreground : root.dim
                y: appsFlick.visibleArea.yPosition * appsScrollTrack.height
                height: Math.max(Style.space(16), appsFlick.visibleArea.heightRatio * appsScrollTrack.height)
              }

              MouseArea {
                id: scrollDragArea
                anchors.fill: parent
                property real dragStartMouseY: 0
                property real dragStartContentY: 0

                function scrollableHeight() {
                  return Math.max(0, appsFlick.contentHeight - appsFlick.height)
                }

                onPressed: (mouse) => {
                  var trackRange = appsScrollTrack.height - appsScrollThumb.height
                  var clickedOutsideThumb = mouse.y < appsScrollThumb.y || mouse.y > appsScrollThumb.y + appsScrollThumb.height
                  if (clickedOutsideThumb && trackRange > 0) {
                    var ratio = Math.max(0, Math.min(1, (mouse.y - appsScrollThumb.height / 2) / trackRange))
                    appsFlick.contentY = ratio * scrollDragArea.scrollableHeight()
                  }
                  dragStartMouseY = mouse.y
                  dragStartContentY = appsFlick.contentY
                }

                onPositionChanged: (mouse) => {
                  if (!pressed) return
                  var trackRange = appsScrollTrack.height - appsScrollThumb.height
                  if (trackRange <= 0) return
                  var deltaContent = (mouse.y - dragStartMouseY) * scrollDragArea.scrollableHeight() / trackRange
                  appsFlick.contentY = Math.max(0, Math.min(scrollDragArea.scrollableHeight(), dragStartContentY + deltaContent))
                }
              }
            }
          }

          Text {
            textFormat: Text.PlainText
            width: parent.width
            text: root.filteredAppKeys.length + " apps · unpinned apps (\"Default\") follow the Overview tab's global default"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.Wrap
          }
        }
      }
    }
  }
}
