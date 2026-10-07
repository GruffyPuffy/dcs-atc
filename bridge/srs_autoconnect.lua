-- Minimal SRS auto-connect announcer (Saved Games/Scripts/Hooks, server side).
-- Tells joining players where the SRS server is; an SRS client with
-- "Auto Connect" enabled parses the chat line and connects by itself.
-- Voice never touches DCS: this hook only sends one chat message per join.
-- __SRS_HOST__ is replaced by `scripts/dcs.sh srs-autoconnect` at deploy time
-- with the detected LAN IP (e.g. 192.168.1.230).
local SRS_HOST = "__SRS_HOST__"
local SRS_PORT = "5002"

local function announce(player_id)
    if not player_id or player_id == 1 then return end -- 1 = the server host
    net.send_chat_to("SRS Running @ " .. SRS_HOST .. ":" .. SRS_PORT, player_id)
end

-- setUserCallbacks may replace the callback table, so chain whatever another
-- hook (e.g. dcs_state_hook.lua) registered before us instead of clobbering it.
local previous = DCS.getUserCallbacks and DCS.getUserCallbacks() or nil
local callbacks = {}
for name, handler in pairs(previous or {}) do callbacks[name] = handler end

callbacks.onPlayerConnect = function(id)
    if previous and previous.onPlayerConnect then pcall(previous.onPlayerConnect, id) end
    if DCS.isServer() then pcall(announce, id) end
end
callbacks.onPlayerChangeSlot = function(id)
    if previous and previous.onPlayerChangeSlot then pcall(previous.onPlayerChangeSlot, id) end
    if DCS.isServer() then pcall(announce, id) end
end

DCS.setUserCallbacks(callbacks)
log.write('SRS-AutoConnect', log.INFO, 'Announcing SRS at ' .. SRS_HOST .. ':' .. SRS_PORT)
