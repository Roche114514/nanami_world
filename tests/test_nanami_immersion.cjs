const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const localHtml = path.join(__dirname, "../web/world.html");
const installedHtml = path.join(__dirname, "../nanami_world/web/world.html");
const html = fs.readFileSync(fs.existsSync(localHtml) ? localHtml : installedHtml, "utf8");

function section(start, end) {
  const first = html.indexOf(start);
  const last = html.indexOf(end, first);
  assert(first >= 0 && last > first, `Missing world script section: ${start}`);
  return html.slice(first, last);
}

function canvas() {
  const draws = [];
  return {
    draws, save() {}, restore() {}, beginPath() {}, rect() {}, clip() {},
    fillRect(...args) { draws.push(args); },
  };
}

function ambientHarness() {
  const width = 288, height = 288;
  const elements = {
    "stage-wrap": { scrollLeft: 0, scrollTop: 0, clientWidth: width, clientHeight: height },
    "world-canvas": { width, height, clientWidth: width, clientHeight: height,
      offsetLeft: 0, offsetTop: 0, clientLeft: 0, clientTop: 0 },
    "modal-mask": { classList: { contains: () => true } },
  };
  return vm.runInNewContext(section("const EmoteFX =", "const PlayerFeedback =") + "({ EmoteFX, AmbientFX })", {
    el: (id) => elements[id],
    TILE: 48, ELEV_LIFT_PX: 8, MAX_ELEVATION: 16,
    spaceTopPad: () => 0, tileTopY: (_sp, _x, y) => y * 48,
  });
}

test("opaque emotes use visible public actions and clear when the actor enters darkness", () => {
  const { EmoteFX } = ambientHarness();
  const ctx = canvas();
  const actor = { actor_id: "npc", actor_status: "active", expression: { tag: "平静" }, last_act: { seq: 0 } };
  const snap = { space: { space_id: "room", space_kind: "room" }, opaque_minds: true };
  const entities = new Map([["tea", { z: 0, traits: { thermal: { temp: "hot" } } }]]);
  const step = (vis, now) => {
    EmoteFX.begin(snap.space.space_id);
    EmoteFX.draw(ctx, snap, actor, entities, vis, 80, 40, now);
    return EmoteFX.active();
  };
  assert.equal(step(1, 0), false);
  actor.expression = { tag: "开心" };
  actor.runtime_activity = { phase: "engaging", verb: "consume", entity_id: "tea" };
  actor.autonomy = { last_thought: "private" };
  assert.equal(step(1, 100), false);
  actor.last_act = { seq: 1, verb: "consume", entity_id: "tea" };
  assert.equal(step(1, 200), true);
  assert.equal(step(0.2, 300), false);
  assert.equal(step(1, 400), false);
  actor.actor_status = "incapacitated";
  actor.last_act = { seq: 2, verb: "consume", entity_id: "tea" };
  assert.equal(step(1, 4500), false);
});

test("cached water and window particles respect the room mask", () => {
  const { AmbientFX } = ambientHarness();
  const ctx = canvas();
  const space = { space_id: "room", space_kind: "room", width: 6, height: 6, wall_rows: 1 };
  const snap = { space, weather: { current: "rain" }, entities: [] };
  const art = AmbientFX.art();
  const tiles = [[], [{ tid: "water", meta: {}, level: 1 }]];
  AmbientFX.capture(art, tiles, 0, 1, 0, 40, 8, "");
  AmbientFX.addWindow(art, 0, 1);
  assert.equal(art.rows[1].length, 1);
  assert.equal(art.windows[0].panes.length, 4);
  const mask = new Float32Array(36);
  AmbientFX.begin(snap, 500);
  AmbientFX.terrain(ctx, snap, art, mask, 500);
  assert.equal(ctx.draws.length, 0);
  assert.equal(AmbientFX.active(), false);
  mask[6] = 1;
  AmbientFX.terrain(ctx, snap, art, mask, 500);
  assert(ctx.draws.length > 10);
  assert.equal(AmbientFX.active(), true);
});

test("wet shoes leave three indoor marks and the marks expire after 1.4 seconds", () => {
  const { AmbientFX } = ambientHarness();
  const ctx = canvas();
  const town = { space: { space_id: "town", space_kind: "town", width: 6, height: 6 },
    weather: { current: "rain" }, entities: [] };
  const room = { space: { space_id: "room", space_kind: "room", width: 6, height: 6 }, entities: [] };
  const art = AmbientFX.art();
  AmbientFX.begin(town, 100);
  AmbientFX.footprint(town, 1, 1, "right", 100);
  AmbientFX.begin(room, 200);
  for (let i = 0; i < 4; i++) AmbientFX.footprint(room, i, 2, "right", 200 + 100 * i);
  AmbientFX.terrain(ctx, room, art, null, 550);
  assert.equal(ctx.draws.length, 9);
  ctx.draws.length = 0;
  AmbientFX.begin(room, 1900);
  AmbientFX.terrain(ctx, room, art, null, 1900);
  assert.equal(ctx.draws.length, 0);
});

test("Web Audio waits for a gesture, fades loops on travel and cleans up when hidden", () => {
  const loops = [], timers = [], gains = [];
  class AudioParam {
    targets = [];
    ramps = [];
    setTargetAtTime(value) { this.targets.push(value); }
    setValueAtTime() {}
    exponentialRampToValueAtTime(value) { this.ramps.push(value); }
  }
  class AudioNode {
    connect() {}
    disconnect() { this.disconnected = true; }
    start() { if (this.loop) loops.push(this); }
    stop(at) {
      this.stopAt = at;
      if (at == null) this.onended?.();
    }
  }
  let audio;
  class AudioContext {
    constructor() { audio = this; }
    currentTime = 1;
    sampleRate = 48;
    state = "running";
    createGain() {
      const node = new AudioNode();
      node.gain = new AudioParam();
      gains.push(node.gain);
      return node;
    }
    createBiquadFilter() { return Object.assign(new AudioNode(), { frequency: new AudioParam() }); }
    createBufferSource() { return new AudioNode(); }
    createOscillator() { return Object.assign(new AudioNode(), { frequency: new AudioParam() }); }
    createBuffer() { return { getChannelData: () => new Float32Array(96) }; }
    async suspend() { this.state = "suspended"; }
  }
  const state = { soundEnabled: true, rainSoundEnabled: true, view: "world" };
  const doc = { hidden: false };
  const fx = vm.runInNewContext(section("const SoundFX =", "const TransitionFX =") + "SoundFX", {
    state, document: doc, window: { AudioContext }, performance,
    setTimeout: (callback) => timers.push(callback), clearTimeout() {},
  });
  const rain = { space: { space_id: "town", space_kind: "town" }, weather: { current: "rain" } };
  fx.syncSpace(rain, { water: 1, record: 1 });
  assert.equal(audio, undefined);
  fx.unlock();
  fx.syncSpace(rain, { water: 1, record: 1 });
  assert.equal(loops.length, 2);
  fx.syncSpace({ space: { space_id: "room" }, weather: { current: "rain" } });
  assert.equal(loops.length, 3);
  assert(loops.slice(0, 2).every((src) => src.stopAt > audio.currentTime));
  doc.hidden = true;
  fx.pageHidden(true);
  fx.syncSpace(rain, { water: 1 });
  const busGains = gains.filter((gain) => gain.targets.length > 0);
  const envGains = gains.filter((gain) => gain.targets.length === 0);
  assert(busGains.length >= 4);
  assert(busGains.every((gain) => gain.targets.at(-1) === 0));
  assert(envGains.every((gain) => gain.ramps.at(-1) <= 0.001));
  for (const callback of timers) callback();
  assert(loops.every((src) => src.disconnected));
  assert.equal(audio.state, "suspended");
});
