const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");

const localHtml = path.join(__dirname, "../web/world.html");
const installedHtml = path.join(__dirname, "../nanami_world/web/world.html");

test("opening the log or switching the cinema camera does not send a chat heartbeat", () => {
  const html = fs.readFileSync(fs.existsSync(localHtml) ? localHtml : installedHtml, "utf8");
  const start = html.indexOf('  let chatPresenceActor = "";');
  const end = html.indexOf("  const triggerRoamInteract =", start);
  assert(start >= 0 && end > start);

  const calls = [];
  const listeners = {};
  const input = { addEventListener(event, handler) { listeners[event] = handler; } };
  const elements = { "chat-input": input, "btn-dock-close": {}, "btn-dock-reopen": {} };
  const document = { hidden: false, activeElement: null, addEventListener() {} };
  const first = { actor_id: "a", space_id: "yard", x: 4, y: 4 };
  const second = { actor_id: "b", space_id: "yard", x: 5, y: 4 };
  const player = { actor_id: "player", is_player: true, space_id: "yard", x: 4, y: 5 };
  const state = {
    view: "world", uiMode: "play", dockOpen: false, playerId: "", greetingActorId: "",
    snap: { active_actor_id: first.actor_id, actors: [first, second], external_overlay: { view_only: true } },
  };
  let heartbeat;
  vm.runInNewContext(html.slice(start, end), {
    state, document, el: (id) => elements[id], renderViewShell() {}, renderDialogueDock() {},
    runAction(action, values) {
      assert.equal(action, "user_chat_presence");
      calls.push([values.actor_id, values.active]);
      return Promise.resolve();
    },
    setInterval(callback, delay) {
      assert.equal(delay, 8000);
      heartbeat = callback;
    },
  });

  elements["btn-dock-reopen"].onclick();
  heartbeat();
  state.snap.active_actor_id = second.actor_id;
  heartbeat();
  assert.deepEqual(calls, []);

  state.uiMode = "roam";
  state.playerId = player.actor_id;
  state.snap.actors.push(player);
  state.snap.external_overlay.view_only = false;
  heartbeat();
  assert.deepEqual(calls, []);

  document.activeElement = input;
  listeners.focus();
  heartbeat();
  document.activeElement = null;
  listeners.blur();
  heartbeat();
  assert.deepEqual(calls, [["b", true], ["b", true], ["b", false]]);
});
