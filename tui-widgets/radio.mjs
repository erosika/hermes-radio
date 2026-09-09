import { spawn } from 'node:child_process'
import { readFile } from 'node:fs/promises'
import { createConnection } from 'node:net'
import { homedir } from 'node:os'
import { delimiter, join } from 'node:path'

const WIDTH = 62
// Dialog draws a 1-cell border and 2 cells of horizontal padding on each side.
const INNER = WIDTH - 6
const BARS = 24
const TITLE_WIDTH = INNER - BARS - 1
const TIME_WIDTH = 13
const TICK_MS = 167
const STALE_MS = 3_000
const STATUS_MS = 4_000
const SPAWN_WAIT_MS = 8_000
const REQUEST_TIMEOUT_MS = 30_000
const DIAL = '○◘◓◑◒●'
const OFF_LINE = 'radio off  ·  /radio play <station>'
const NOT_INSTALLED = 'no radio daemon: enable the hermes-radio plugin'
const HELP = 'play soma crate local pause skip mute vol rec mic search stations stop'
// Braille dot masks accumulated from the bottom row up: 0, 1, 2, 3, 4 rows lit.
const CELL_MASKS = [0x00, 0xc0, 0xe4, 0xf6, 0xff]

export function brailleCell(level) {
  const v = Number.isFinite(level) ? Math.min(1, Math.max(0, level)) : 0

  return String.fromCharCode(0x2800 | CELL_MASKS[Math.round(v * 4)])
}

export function brailleBars(levels, width = BARS) {
  const view = Array.isArray(levels) ? levels.slice(-width) : []

  return view.map(brailleCell).join('').padStart(width, '⠀')
}

function hermesHome() {
  const raw = (process.env.HERMES_HOME || '').trim()

  if (!raw) {
    return join(homedir(), '.hermes')
  }

  return raw.replace(/^~(?=$|\/)/, homedir())
}

const radioDir = () => join(hermesHome(), 'radio')
const socketPath = () => join(radioDir(), 'control.sock')
const statePath = () => join(radioDir(), 'state.json')
const launcherPath = () => join(radioDir(), 'daemon.json')

function fit(text, width) {
  const chars = [...String(text ?? '')]

  if (chars.length > width) {
    return chars.slice(0, Math.max(0, width - 1)).join('') + '…'
  }

  return chars.join('') + ' '.repeat(width - chars.length)
}

function titleOf(snap) {
  const artist = snap.artist || ''
  const title = snap.title || ''
  let display = ''

  if (artist && artist !== 'Unknown') {
    display = title ? `${artist} — ${title}` : artist
  } else if (title && title !== '...' && title !== 'channel.mp3') {
    display = title
  }

  if (snap.source_mode === 'stream' && snap.station_name) {
    display = display && display !== snap.station_name ? `${snap.station_name} — ${display}` : snap.station_name
  }

  return display || snap.station_name || '...'
}

function mmss(seconds) {
  const n = Math.max(0, Math.floor(seconds))

  return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, '0')}`
}

function dialOf(volume) {
  const vol = Math.max(0, Math.min(100, Math.round(Number(volume) || 0)))

  return `${DIAL[Math.min(5, Math.floor((vol * 6) / 101))]} ${String(vol).padStart(3)}`
}

const isLive = snap => !Number.isFinite(snap.position) || !Number.isFinite(snap.duration) || snap.duration <= 0

const isFresh = snap => Number.isFinite(snap.updated_at) && Date.now() - snap.updated_at * 1000 <= STALE_MS

async function readState() {
  return JSON.parse(await readFile(statePath(), 'utf8'))
}

function request(message, timeoutMs = REQUEST_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    const sock = createConnection(socketPath())
    let buffer = ''
    let done = false

    const finish = (settle, value) => {
      if (done) {
        return
      }

      done = true
      clearTimeout(timer)
      sock.destroy()
      settle(value)
    }

    const timer = setTimeout(() => finish(reject, new Error('radio daemon timed out')), timeoutMs)

    sock.setEncoding('utf8')
    sock.on('connect', () => sock.write(`${JSON.stringify(message)}\n`))
    sock.on('data', chunk => {
      buffer += chunk
      const nl = buffer.indexOf('\n')

      if (nl < 0) {
        return
      }

      try {
        finish(resolve, JSON.parse(buffer.slice(0, nl)))
      } catch {
        finish(reject, new Error('bad reply from radio daemon'))
      }
    })
    sock.on('error', err => finish(reject, err))
    sock.on('close', () => finish(reject, new Error('radio daemon closed the connection')))
  })
}

const isAbsent = err => err && (err.code === 'ENOENT' || err.code === 'ECONNREFUSED')

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))

async function readLauncher() {
  try {
    const launcher = JSON.parse(await readFile(launcherPath(), 'utf8'))

    return launcher && typeof launcher.python === 'string' && typeof launcher.script === 'string' ? launcher : null
  } catch {
    return null
  }
}

function spawnDaemon(launcher) {
  const env = { ...process.env }

  if (launcher.hermes_root) {
    env.PYTHONPATH = env.PYTHONPATH ? `${launcher.hermes_root}${delimiter}${env.PYTHONPATH}` : launcher.hermes_root
  }

  let failure = null
  const child = spawn(launcher.python, [launcher.script], { detached: true, env, stdio: 'ignore' })

  child.on('error', err => {
    failure = err
  })
  child.unref()

  return () => failure
}

async function call(method, params, start = true) {
  const message = { id: 1, method, params }

  try {
    return await request(message)
  } catch (err) {
    if (!isAbsent(err)) {
      throw err
    }
  }

  if (!start) {
    return { id: 1, ok: true, result: method === 'stop' ? 'Radio is already off' : 'radio is off' }
  }

  const launcher = await readLauncher()

  if (!launcher) {
    throw new Error(NOT_INSTALLED)
  }

  const spawnFailure = spawnDaemon(launcher)
  const deadline = Date.now() + SPAWN_WAIT_MS

  for (;;) {
    await sleep(250)

    const failed = spawnFailure()

    if (failed) {
      throw new Error(`could not start radio daemon: ${failed.message}`)
    }

    try {
      return await request(message)
    } catch (err) {
      if (!isAbsent(err)) {
        throw err
      }

      if (Date.now() > deadline) {
        throw new Error('radio daemon did not start')
      }
    }
  }
}

function crateParams(tokens) {
  const params = {}

  for (const token of tokens) {
    const year = /^\d{4}$/.test(token) ? Number(token) : NaN

    if (year >= 1900 && year <= 2020) {
      ;(params.decades ??= []).push(Math.floor(year / 10) * 10)
    } else if (/^(slow|fast|weird)$/i.test(token)) {
      ;(params.moods ??= []).push(token.toLowerCase())
    } else if (/^[a-z]{3}$/i.test(token)) {
      params.country = token.toUpperCase()
    }
  }

  return params
}

function volumeCommand(arg) {
  if (/^[+-]\d+$/.test(arg)) {
    return { method: 'adjust_volume', params: { delta: Number(arg) } }
  }

  if (/^\d+$/.test(arg)) {
    return { method: 'set_volume', params: { level: Math.min(100, Number(arg)) } }
  }

  return { local: 'usage: /radio vol <0-100> | +N | -N' }
}

async function recordingCommand(tokens) {
  const action = (tokens[0] || '').toLowerCase()

  if (action === 'start') {
    return { method: 'start_recording', params: { path: tokens.slice(1).join(' ') } }
  }

  if (action === 'stop') {
    return { method: 'stop_recording', params: {} }
  }

  if (action) {
    return { local: 'usage: /radio rec [start|stop]' }
  }

  let recording = false

  try {
    recording = (await readState()).recording === true
  } catch {
    // No state file means nothing is recording.
  }

  return { method: recording ? 'stop_recording' : 'start_recording', params: {} }
}

async function resolveCommand(tail) {
  const [verb, ...rest] = tail.split(/\s+/)
  const cmd = verb.toLowerCase()
  const arg = rest.join(' ')

  switch (cmd) {
    case 'play':
      if (!arg) {
        return { local: 'usage: /radio play <name|url>' }
      }

      return /^https?:\/\//i.test(arg)
        ? { method: 'play_stream', params: { url: arg } }
        : { method: 'play_station', params: { query: arg } }
    case 'soma':
    case 'somafm':
      return { method: 'play_somafm', params: { channel_id: rest[0] || '' } }
    case 'crate':
    case 'dig':
      return { method: 'play_crate', params: crateParams(rest) }
    case 'local':
      return arg ? { method: 'play_local', params: { path: arg } } : { local: 'usage: /radio local <path>' }
    case 'pause':
      return { method: 'toggle_pause', params: {}, start: false }
    case 'skip':
    case 'next':
      return { method: 'skip', params: {}, start: false }
    case 'mute':
      return { method: 'toggle_mute', params: {}, start: false }
    case 'vol':
    case 'volume':
      return volumeCommand(rest[0] || '')
    case 'rec':
    case 'record':
      return recordingCommand(rest)
    case 'mic':
      return { method: 'mic_break', params: arg ? { text: arg } : {} }
    case 'search':
      return arg ? { method: 'search', params: { query: arg, source: 'radio_browser' } } : { local: 'usage: /radio search <query>' }
    case 'stations':
      return { method: 'stations', params: {} }
    case 'viz':
      return { local: 'viz presets are not available in the dock; use hermes radio viz' }
    case 'stop':
    case 'off':
      return { method: 'stop', params: {}, start: false }
    case 'help':
      return { local: HELP }
    default:
      return { local: `unknown radio command: ${cmd}  (try /radio help)` }
  }
}

const nameOf = item => (typeof item === 'string' ? item : (item && (item.name || item.title || item.id)) || '')

const listSummary = (items, noun) =>
  items.length ? `${items.length} ${noun}: ${items.map(nameOf).filter(Boolean).join(', ')}` : `no ${noun}`

function describe(reply) {
  if (!reply || typeof reply !== 'object') {
    return { text: 'no reply from radio daemon', tone: 'error' }
  }

  if (!reply.ok) {
    return { text: `error: ${reply.error || 'unknown error'}`, tone: 'error' }
  }

  const result = reply.result

  if (typeof result === 'string') {
    return { text: result, tone: 'ok' }
  }

  if (result && Array.isArray(result.channels)) {
    return { text: listSummary(result.channels, 'SomaFM channels'), tone: 'ok' }
  }

  if (result && Array.isArray(result.results)) {
    return { text: listSummary(result.results, 'results'), tone: 'ok' }
  }

  if (result && Array.isArray(result.stations)) {
    return { text: listSummary(result.stations, 'stations'), tone: 'ok' }
  }

  return { text: JSON.stringify(result ?? null), tone: 'ok' }
}

export default function register(sdk) {
  const { Box, Dialog, React, Text, defineWidgetApp, h, isCtrl, updateWidget } = sdk
  let statusSeq = 0

  function setStatus(text, tone, clear = true) {
    const id = ++statusSeq

    updateWidget(app, state => ({ ...state, status: { id, text, tone } }))

    if (!clear) {
      return
    }

    const timer = setTimeout(() => {
      updateWidget(app, state => (state.status && state.status.id === id ? { ...state, status: null } : state))
    }, STATUS_MS)

    timer.unref?.()
  }

  async function runCommand(tail) {
    try {
      const command = await resolveCommand(tail)

      if (command.local) {
        setStatus(command.local, 'muted')

        return
      }

      const reply = await call(command.method, command.params, command.start !== false)
      const { text, tone } = describe(reply)

      setStatus(text, tone)
    } catch (err) {
      setStatus(err && err.message ? err.message : String(err), 'error')
    }
  }

  const toneColor = (t, tone) => (tone === 'error' ? t.color.error : tone === 'ok' ? t.color.ok : t.color.muted)

  function Card({ status, t }) {
    const snapRef = React.useRef(null)
    const levelsRef = React.useRef([])
    const [, setTick] = React.useState(0)

    React.useEffect(() => {
      let alive = true
      let busy = false

      const poll = async () => {
        if (busy) {
          return
        }

        busy = true

        try {
          const snap = await readState()

          if (alive && snap && typeof snap === 'object') {
            snapRef.current = snap

            if (Array.isArray(snap.levels)) {
              levelsRef.current = snap.levels
            }
          }
        } catch (err) {
          // A missing file is a definite "off"; a torn or busy read keeps the last snapshot.
          if (alive && err && err.code === 'ENOENT') {
            snapRef.current = null
          }
        } finally {
          busy = false

          if (alive) {
            setTick(n => n + 1)
          }
        }
      }

      void poll()
      const id = setInterval(() => void poll(), TICK_MS)

      return () => {
        alive = false
        clearInterval(id)
      }
    }, [])

    const snap = snapRef.current
    const active = Boolean(snap && snap.active === true && isFresh(snap))
    const row = (...children) => h(Box, { flexDirection: 'row', height: 1, width: INNER }, ...children)
    const cell = (text, color, extra = {}) => h(Text, { color, wrap: 'truncate', ...extra }, text)

    if (!active) {
      return h(
        Box,
        { flexDirection: 'column', width: INNER },
        row(cell(fit(OFF_LINE, INNER), t.color.muted)),
        row(status ? cell(fit(status.text, INNER), toneColor(t, status.tone)) : cell(' '.repeat(INNER), t.color.muted))
      )
    }

    const paused = snap.paused === true
    const recording = snap.recording === true
    const blinkOn = Math.floor(Date.now() / 500) % 2 === 0
    const time = isLive(snap) ? 'LIVE' : `${mmss(snap.position)} / ${mmss(snap.duration)}`
    const segments = [
      { color: isLive(snap) ? t.color.ok : t.color.text, text: fit(time, TIME_WIDTH) },
      { color: t.color.label, text: `  ${dialOf(snap.volume)}` }
    ]

    if (recording) {
      segments.push({ color: blinkOn ? t.color.error : t.color.muted, text: blinkOn ? '  ● REC' : '  ○ REC' })
    }

    if (paused) {
      segments.push({ color: t.color.muted, text: '  paused' })
    }

    const used = segments.reduce((n, s) => n + [...s.text].length, 0)
    const rest = INNER - used

    segments.push(
      status && rest > 2
        ? { color: toneColor(t, status.tone), text: `  ${fit(status.text, rest - 2)}` }
        : { color: t.color.muted, text: ' '.repeat(Math.max(0, rest)) }
    )

    return h(
      Box,
      { flexDirection: 'column', width: INNER },
      row(
        cell(brailleBars(levelsRef.current, BARS), paused ? t.color.muted : t.color.primary),
        cell(` ${fit(titleOf(snap), TITLE_WIDTH)}`, t.color.text, { bold: true })
      ),
      row(...segments.map(s => cell(s.text, s.color)))
    )
  }

  const app = defineWidgetApp({
    id: 'radio',
    help: 'Hermes Radio dock: /radio [play <name|url> | soma | crate | pause | skip | vol N | rec | stop | ...]',
    mode: 'ambient',
    zone: 'dock-bottom',
    width: WIDTH,
    usage: 'usage: /radio [play <name|url> | soma [channel] | crate [1970 JPN slow] | pause | skip | mute | vol N | rec | mic | search | stations | stop | help]',

    init(arg) {
      const tail = String(arg ?? '').trim()

      if (!tail) {
        return {}
      }

      try {
        void runCommand(tail)
      } catch {
        // runCommand reports its own failures through the status line.
      }

      // The host docks the card after init returns, so the echo rides on the initial state.
      return { status: { id: ++statusSeq, text: `${tail} …`, tone: 'muted' } }
    },

    reduce(state, { ch, key }) {
      return (key && key.escape) || ch === 'q' || (key && isCtrl(key, ch, 'c')) ? null : state
    },

    render({ state, t }) {
      return h(Dialog, { width: WIDTH }, h(Card, { status: (state && state.status) || null, t }))
    }
  })
}
