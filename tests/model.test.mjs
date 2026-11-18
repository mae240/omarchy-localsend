// Run with: node --test tests/
import { test } from "node:test"
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"
import { dirname, join } from "node:path"

const here = dirname(fileURLToPath(import.meta.url))
const source = readFileSync(join(here, "..", "Model.js"), "utf8")
const Model = new Function(source + `
  return { parseEvent, isValidIp, isValidPort, isValidTarget, normalizeDevice, deviceKey, deviceIcon, favoriteEntry,
           sanitizeFavorites, isFavorite, toggleFavorite, mergeDevices, formatBytes, deviceSubtitle, transferFraction,
           isValidFingerprint, fingerprintArgs }
`)()

const phone = { alias: "Pixel", ip: "192.168.0.23", port: 53317, protocol: "https", deviceType: "mobile", deviceModel: "Google", version: "2.1", fingerprint: "AAA" }
const laptop = { alias: "book", ip: "192.168.0.50", port: 53317, protocol: "http", deviceType: "desktop", deviceModel: "", version: "2.0", fingerprint: "" }

test("parseEvent only accepts JSON objects with an event", () => {
  assert.equal(Model.parseEvent("{\"event\":\"done\"}").event, "done")
  assert.equal(Model.parseEvent("[1]"), null)
  assert.equal(Model.parseEvent("nope"), null)
  assert.equal(Model.parseEvent("{\"x\":1}"), null)
})

test("targets must have a sane ip, port and protocol", () => {
  assert.equal(Model.isValidTarget(phone), true)
  assert.equal(Model.isValidTarget({ ...phone, ip: "192.168.0.999" }), false)
  assert.equal(Model.isValidTarget({ ...phone, ip: "1.2.3.4; rm -rf" }), false)
  assert.equal(Model.isValidTarget({ ...phone, port: "53317 --x" }), false)
  assert.equal(Model.isValidTarget({ ...phone, protocol: "ftp" }), false)
  assert.equal(Model.isValidTarget({ ...phone, ip: "fe80::1" }), true)
})

test("normalizeDevice bounds strings and keeps markup as plain data", () => {
  const device = Model.normalizeDevice({ ...phone, alias: "<img src=\"https://example.invalid/t\">\u0007", deviceModel: "x".repeat(200) })
  assert.equal(device.alias, "<img src=\"https://example.invalid/t\">")
  assert.equal(device.deviceModel.length, 64)
  assert.equal(Model.normalizeDevice({ ...phone, alias: "   " }), null)
  assert.equal(Model.normalizeDevice({ ...phone, ip: "nope" }), null)
})

test("deviceKey prefers the fingerprint, falls back to address", () => {
  assert.equal(Model.deviceKey(phone), "fp:AAA")
  assert.equal(Model.deviceKey(laptop), "addr:192.168.0.50:53317")
})

test("favorites toggle, persist a bounded record and merge with live devices", () => {
  let favorites = Model.toggleFavorite([], phone)
  assert.equal(favorites.length, 1)
  assert.equal(Model.isFavorite(favorites, phone), true)
  assert.deepEqual(Object.keys(favorites[0]).sort(), ["alias", "deviceModel", "deviceType", "fingerprint", "ip", "port", "protocol"])
  favorites = Model.toggleFavorite(favorites, phone)
  assert.equal(favorites.length, 0)

  favorites = Model.toggleFavorite([], { ...phone, ip: "10.0.0.9" })
  const rows = Model.mergeDevices([phone, laptop], favorites)
  assert.deepEqual(rows.map(r => r.alias), ["Pixel", "book"])
  assert.equal(rows[0].favorite, true)
  assert.equal(rows[0].online, true)
  assert.equal(rows[0].ip, "192.168.0.23", "a live favorite takes the live address")
  assert.equal(rows[1].favorite, false)

  const offline = Model.mergeDevices([laptop], favorites)
  assert.equal(offline[0].alias, "Pixel")
  assert.equal(offline[0].online, false)
  assert.equal(offline[0].ip, "10.0.0.9")
})

test("sanitizeFavorites drops junk from settings and caps the list", () => {
  const junk = [phone, null, "x", { alias: "bad", ip: "::::::::::::::::::::::::::::::::::::::::::::", port: 1, protocol: "https" }]
  for (let i = 0; i < 30; i++) junk.push({ ...laptop, alias: "n" + i, ip: "10.0.0." + (i + 1) })
  const list = Model.sanitizeFavorites(junk)
  assert.equal(list.length, 20)
  assert.equal(list[0].alias, "Pixel")
})

test("formatBytes and transferFraction", () => {
  assert.equal(Model.formatBytes(19), "19 B")
  assert.equal(Model.formatBytes(3000019), "3.0 MB")
  assert.equal(Model.formatBytes(1500), "1.5 kB")
  assert.equal(Model.formatBytes(2500000000), "2.5 GB")
  assert.equal(Model.formatBytes(1500, (v, d) => Number(v).toFixed(d).replace(".", ",")), "1,5 kB")
  assert.equal(Model.transferFraction({ totalSent: 50, totalSize: 200 }), 0.25)
  assert.equal(Model.transferFraction({ totalSent: 500, totalSize: 200 }), 1)
  assert.equal(Model.transferFraction(null), 0)
})

test("deviceSubtitle and deviceIcon", () => {
  assert.equal(Model.deviceSubtitle(phone), "192.168.0.23 · Google")
  assert.equal(Model.deviceSubtitle({ ...laptop, port: 53320 }), "192.168.0.50:53320")
  assert.equal(Model.deviceIcon("mobile"), "󰄜")
  assert.equal(Model.deviceIcon("whatever"), "󰌢")
})

test("fingerprints that could act as options or carry junk are dropped", () => {
  assert.equal(Model.isValidFingerprint("10282E962A1D"), true)
  assert.equal(Model.isValidFingerprint(""), true)
  assert.equal(Model.isValidFingerprint("a b"), false)
  assert.equal(Model.isValidFingerprint("x".repeat(129)), false)
  assert.equal(Model.normalizeDevice({ ...phone, fingerprint: "AA; rm" }).fingerprint, "")
  assert.equal(Model.normalizeDevice({ ...phone, verified: "yes" }).verified, false)
  assert.equal(Model.normalizeDevice({ ...phone, verified: true }).verified, true)
})

test("an https favorite is not taken over by an http device with its fingerprint", () => {
  const favorites = Model.toggleFavorite([], phone)
  const impostor = { ...phone, ip: "192.168.0.66", protocol: "http" }
  const rows = Model.mergeDevices([impostor], favorites)
  assert.equal(rows.length, 2)
  const favorite = rows.find(r => r.favorite)
  assert.equal(favorite.online, false)
  assert.equal(favorite.ip, "192.168.0.23")
  assert.equal(favorite.protocol, "https")
  const other = rows.find(r => !r.favorite)
  assert.equal(other.ip, "192.168.0.66")
  assert.equal(other.fingerprint, "", "the impostor keeps no claim on the fingerprint")
  assert.equal(Model.isFavorite(favorites, other), false)
})

test("an https favorite follows its device to a new https address", () => {
  const favorites = Model.toggleFavorite([], phone)
  const rows = Model.mergeDevices([{ ...phone, ip: "192.168.0.77" }], favorites)
  assert.equal(rows.length, 1)
  assert.equal(rows[0].online, true)
  assert.equal(rows[0].ip, "192.168.0.77")
})

test("sends over https are pinned to the fingerprint, http sends are not", () => {
  assert.deepEqual(Model.fingerprintArgs(phone), ["--fingerprint=AAA"])
  assert.deepEqual(Model.fingerprintArgs({ ...phone, protocol: "http" }), [])
  assert.deepEqual(Model.fingerprintArgs({ ...phone, fingerprint: "" }), [])
  assert.deepEqual(Model.fingerprintArgs({ ...phone, fingerprint: "a b" }), [])
})
