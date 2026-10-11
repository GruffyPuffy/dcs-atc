-- Minimal SRS auto-connect announcer (Saved Games/Scripts/Hooks, server side).
-- Tells joining players where the SRS server is; an SRS client with
-- "Auto Connect" enabled parses the chat line and connects by itself.
-- Voice never touches DCS: this hook only sends one chat message per join.
--
-- Two announcement formats are understood by the SRS client:
--   "SRS Running on <port>"        -> the client uses the SAME host it used to
--                                     reach DCS (net.get_server_host()), so it
--                                     works for LAN *and* internet clients with
--                                     no config. This is the default.
--   "SRS Running @ <host>:<port>"  -> an explicit address. Set SRS_HOST (via
--                                     `SRS_HOST=... ./scripts/dcs.sh
--                                     srs-autoconnect`) to force this, e.g. a
--                                     public hostname.
-- __SRS_HOST__ is replaced at deploy time (empty by default = port-only form).
local SRS_HOST = "__SRS_HOST__"
local SRS_PORT = "5002"

local function announce(player_id)
    if not player_id or player_id == 1 then return end -- 1 = the server host
    local message
    if SRS_HOST ~= "" then
        message = "SRS Running @ " .. SRS_HOST .. ":" .. SRS_PORT
    else
        message = "SRS Running on " .. SRS_PORT
    end
    net.send_chat_to(message, player_id)
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
local where = (SRS_HOST ~= "") and (SRS_HOST .. ':' .. SRS_PORT)
    or ('port ' .. SRS_PORT .. ' (client reuses the DCS host)')
log.write('SRS-AutoConnect', log.INFO, 'Announcing SRS at ' .. where)
