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

function startRoam(x, y) {
  const space = {
    space_id: "town",
    width: 7,
    height: 7,
    elevations: { "2,2": 1, "3,2": 1, "4,2": 1, "2,3": 1 },
    stairs: { "2,3": "both", "3,2": "ns" },
  };
  const player = { actor_id: "player", space_id: "town", x: Math.round(x), y: Math.round(y), move_seq: 0 };
  const state = {
    playerId: player.actor_id,
    uiMode: "roam",
    snap: { space, active_space_id: space.space_id, actors: [player], entities: [] },
  };
  const position = { active: true, sid: space.space_id, x, y, gx: player.x, gy: player.y, dir: "down", dist: 0 };
  const sent = [];
  let serverX = player.x, serverY = player.y;
  const context = {
    state,
    ROAM_POS: position,
    roamInteractBusy: false,
    refreshLocalRoamPrompt() {},
    resolveTileMeta(_space, tx, ty) {
      return { tid: "stone_tile", meta: { walkable: tx !== 5 || ty !== 2 } };
    },
    SoundFX: { footstep() {} },
    AmbientFX: { footprint() {} },
    performance,
  };
  const roam = vm.runInNewContext(
    section("function isCellWalkableLocal(", "function refreshLocalRoamPrompt(") +
    section("function cellElev(", "function sampleSmoothElev(") +
    section("  const dirMap = ", "  let lastRafAt = ") +
    section("  const ROAM_SPEED = ", "  const flushRoamSync = ") +
    section("  const updateRoamMotion = ", '  window.addEventListener("keydown", (ev) => {') +
    "({ keysHeld, roamSyncQueue, updateRoamMotion, canStepElevLocal })",
    context,
  );
  context.flushRoamSync = () => {
    for (const [dx, dy] of roam.roamSyncQueue.splice(0)) {
      const nx = serverX + dx, ny = serverY + dy;
      assert.equal(Math.abs(dx) + Math.abs(dy), 1);
      assert(roam.canStepElevLocal(space, serverX, serverY, nx, ny));
      assert(context.isCellWalkableLocal(nx, ny));
      sent.push([dx, dy]);
      serverX = nx;
      serverY = ny;
    }
  };
  return {
    sent,
    move(keys, frames = 1) {
      roam.keysHeld.splice(0, roam.keysHeld.length, ...keys);
      for (let i = 0; i < frames; i++) roam.updateRoamMotion(1 / 60);
      assert.deepEqual([player.x, player.y], [serverX, serverY]);
      assert.equal(player.move_seq, sent.length);
      return [player.x, player.y];
    },
  };
}

test("a player can turn between offset stairs while overlapping the corner", () => {
  const roam = startRoam(2.37, 2.76);
  assert.deepEqual(roam.move(["d"], 5), [3, 3]);
  assert.deepEqual(roam.move(["w"], 10), [3, 2]);
  assert.deepEqual(roam.sent, [[1, 0], [0, -1]]);
});

test("both stair steps in one frame are sent in the validated order", () => {
  const roam = startRoam(2.46, 2.54);
  assert.deepEqual(roam.move(["d", "w"]), [3, 2]);
  assert.deepEqual(roam.sent, [[1, 0], [0, -1]]);
});

test("a bare cliff edge and a wall still block player steps", () => {
  const roam = startRoam(4, 2);
  assert.deepEqual(roam.move(["s"], 16), [4, 2]);
  assert.deepEqual(roam.move(["d"], 16), [4, 2]);
  assert.deepEqual(roam.sent, []);
});
