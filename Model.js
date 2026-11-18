// Pure helpers for the LocalSend bar plugin. No QML imports, so
// tests/model.test.mjs can load this file in node as-is.

var MAX_FAVORITES = 20

// Control characters plus the Unicode line and paragraph separators, built
// at runtime because the two separators may not appear literally in source.
var CONTROL_CHARS = new RegExp("[\\x00-\\x1f\\x7f" + String.fromCharCode(8232) + String.fromCharCode(8233) + "]", "g")

function bound(value, limit) {
  var text = String(value || "").replace(CONTROL_CHARS, "").trim()
  return text.length > limit ? text.substring(0, limit - 1) + "…" : text
}

function parseEvent(line) {
  try {
    var parsed = JSON.parse(String(line || ""))
    return parsed && typeof parsed === "object" && typeof parsed.event === "string" ? parsed : null
  } catch (e) {
    return null
  }
}

function isValidIp(ip) {
  var text = String(ip || "")
  if (/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.test(text)) {
    var parts = text.split(".")
    for (var i = 0; i < 4; i++) if (parseInt(parts[i], 10) > 255) return false
    return true
  }
  return /^[0-9a-fA-F:]{2,39}$/.test(text) && text.indexOf(":") !== -1
}

function isValidPort(port) {
  var n = parseInt(String(port), 10)
  return isFinite(n) && n >= 1 && n <= 65535 && String(n) === String(port)
}

// Only a device record that can safely become helper arguments is a target.
function isValidTarget(device) {
  return !!device && isValidIp(device.ip) && isValidPort(device.port)
    && (device.protocol === "http" || device.protocol === "https")
}

// A device event from the helper, with every displayed string bounded again
// on this side of the pipe.
function normalizeDevice(raw) {
  if (!raw || typeof raw !== "object") return null
  var device = {
    alias: bound(raw.alias, 64),
    ip: String(raw.ip || ""),
    port: parseInt(String(raw.port), 10),
    protocol: raw.protocol === "http" ? "http" : "https",
    deviceType: String(raw.deviceType || "desktop"),
    deviceModel: bound(raw.deviceModel, 64),
    version: bound(raw.version, 16),
    fingerprint: bound(raw.fingerprint, 128)
  }
  if (device.alias === "" || !isValidTarget(device)) return null
  return device
}

function deviceKey(device) {
  if (!device) return ""
  if (device.fingerprint) return "fp:" + device.fingerprint
  return "addr:" + device.ip + ":" + device.port
}

function deviceIcon(type) {
  var value = String(type || "").toLowerCase()
  if (value === "mobile") return "󰄜"
  if (value === "web") return "󰖟"
  if (value === "headless") return "󰆍"
  if (value === "server") return "󰒋"
  return "󰌢"
}

// What is persisted for a favorite: enough to show it and to reach it again.
function favoriteEntry(device) {
  return {
    alias: device.alias,
    ip: device.ip,
    port: device.port,
    protocol: device.protocol,
    deviceType: device.deviceType,
    deviceModel: device.deviceModel,
    fingerprint: device.fingerprint
  }
}

function sanitizeFavorites(list) {
  var out = []
  if (!list || typeof list.length !== "number") return out
  for (var i = 0; i < list.length && out.length < MAX_FAVORITES; i++) {
    var device = normalizeDevice(list[i])
    if (device) out.push(favoriteEntry(device))
  }
  return out
}

function isFavorite(favorites, device) {
  var key = deviceKey(device)
  for (var i = 0; i < favorites.length; i++) if (deviceKey(favorites[i]) === key) return true
  return false
}

function toggleFavorite(favorites, device) {
  var key = deviceKey(device)
  var next = []
  var removed = false
  for (var i = 0; i < favorites.length; i++) {
    if (deviceKey(favorites[i]) === key) { removed = true; continue }
    next.push(favorites[i])
  }
  if (!removed) next.unshift(favoriteEntry(device))
  return next.slice(0, MAX_FAVORITES)
}

// The list the panel shows: favorites first, each updated from the live
// device when it is on the network and marked offline when it is not,
// then every other device that was found, both halves by alias.
function mergeDevices(discovered, favorites) {
  var byKey = {}
  for (var i = 0; i < discovered.length; i++) byKey[deviceKey(discovered[i])] = discovered[i]
  var rows = []
  var used = {}
  for (var f = 0; f < favorites.length; f++) {
    var key = deviceKey(favorites[f])
    var live = byKey[key]
    var base = live || favorites[f]
    used[key] = true
    rows.push(withFlags(base, true, !!live))
  }
  var others = []
  for (var d = 0; d < discovered.length; d++) {
    var k = deviceKey(discovered[d])
    if (used[k]) continue
    used[k] = true
    others.push(withFlags(discovered[d], false, true))
  }
  var byAlias = function(a, b) { return a.alias.toLowerCase() < b.alias.toLowerCase() ? -1 : (a.alias.toLowerCase() > b.alias.toLowerCase() ? 1 : 0) }
  rows.sort(byAlias)
  others.sort(byAlias)
  return rows.concat(others)
}

function withFlags(device, favorite, online) {
  var copy = {}
  for (var key in device) copy[key] = device[key]
  copy.favorite = favorite
  copy.online = online
  copy.key = deviceKey(device)
  return copy
}

function formatBytes(bytes, formatNumber) {
  var n = Number(bytes) || 0
  var fmt = typeof formatNumber === "function" ? formatNumber : function(v, d) { return Number(v).toFixed(d) }
  if (n < 1000) return fmt(n, 0) + " B"
  if (n < 1000000) return fmt(n / 1000, n < 10000 ? 1 : 0) + " kB"
  if (n < 1000000000) return fmt(n / 1000000, n < 10000000 ? 1 : 0) + " MB"
  return fmt(n / 1000000000, 1) + " GB"
}

function deviceSubtitle(device) {
  var parts = []
  if (device.ip) parts.push(device.ip + (device.port && device.port !== 53317 ? ":" + device.port : ""))
  if (device.deviceModel) parts.push(device.deviceModel)
  return parts.join(" · ")
}

// Progress of the running transfer, 0..1.
function transferFraction(progress) {
  if (!progress || !progress.totalSize) return 0
  return Math.max(0, Math.min(1, (Number(progress.totalSent) || 0) / Number(progress.totalSize)))
}
