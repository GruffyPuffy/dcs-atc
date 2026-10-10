-- Mission-side body for the ATC state bridge.
--
-- Executed by dcs_state_hook.lua via a_do_script inside net.dostring_in('mission').
-- This runs in the REAL mission scripting environment, where the SSE globals
-- (coalition, Group, coord, net) exist. `req` is injected by the hook and holds
-- the parsed request table. Must return a JSON string (built with net.lua2json).
--
-- This file is read fresh on every request, so edits take effect without
-- restarting DCS (the hook falls back to its embedded copy if this file is
-- missing). Deploy with: ./scripts/dcs.sh bridge

local function unit_json(u)
    local p = u:getPosition()
    local lat, lon, alt = coord.LOtoLL(p.p)
    -- p.x is the unit's forward vector; DCS world is x=north, z=east, y=up.
    local heading = math.deg(math.atan2(p.x.z, p.x.x)) % 360
    -- Ground speed (m/s) from the velocity vector, for the low-and-fast check.
    local v = p.v or { x = 0, y = 0, z = 0 }
    local speed = math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z)
    return {
        name = u:getName(),
        type = u:getTypeName(),
        x = p.p.x, y = p.p.y, z = p.p.z,
        lat = lat, lon = lon, alt = alt,
        heading = heading,
        speed = speed,
        player = u:getPlayerName() or "",
    }
end

local function group_json(g, coa)
    local units = {}
    for _, u in ipairs(g:getUnits() or {}) do
        units[#units + 1] = unit_json(u)
    end
    return {
        name = g:getName(),
        coalition = coa,
        category = g:getCategory(),
        units = units,
    }
end

local function snapshot()
    local out = { groups = {}, airbases = {} }
    for _, coa in ipairs({0, 1, 2}) do
        local ok, groups = pcall(coalition.getGroups, coa)
        if ok and groups then
            for _, g in ipairs(groups) do
                out.groups[#out.groups + 1] = group_json(g, coa)
            end
        end
        local ok2, bases = pcall(coalition.getAirbases, coa)
        if ok2 and bases then
            for _, b in ipairs(bases) do
                local p = b:getPoint()
                local lat, lon, alt = coord.LOtoLL(p)
                out.airbases[#out.airbases + 1] = {
                    name = b:getName(),
                    coalition = coa,
                    x = p.x, y = p.y, z = p.z,
                    lat = lat, lon = lon, alt = alt,
                }
            end
        end
    end
    return out
end

local function envelope(ok, payload)
    if ok then
        return net.lua2json({ v = 1, id = req.id, ok = true, result = payload })
    end
    return net.lua2json({ v = 1, id = req.id, ok = false, error = payload })
end

-- Player-slot callsigns from the mission definition (env.mission). Each slot
-- has a callsign table {name=..., nr=...}; e.g. name="Colt", nr=1 -> "Colt11".
local function callsigns()
    local out = {}
    local function scan(coa_name, coa)
        if type(coa) ~= 'table' or type(coa.country) ~= 'table' then return end
        for _, country in ipairs(coa.country) do
            for _, cat in ipairs({'plane', 'helicopter'}) do
                local groups = country[cat] and country[cat].group
                if type(groups) == 'table' then
                    for _, g in ipairs(groups) do
                        for _, u in ipairs(g.units or {}) do
                            if u.skill == 'Client' or u.skill == 'Player' then
                                local cs = u.callsign
                                local name, nr = '', ''
                                if type(cs) == 'table' then
                                    name = tostring(cs.name or '')
                                    nr = tostring(cs.nr or '')
                                elseif type(cs) == 'string' then
                                    name = cs
                                end
                                out[#out + 1] = {
                                    name = name,
                                    nr = nr,
                                    full = name .. nr,
                                    type = tostring(u.type or ''),
                                    coalition = coa_name,
                                }
                            end
                        end
                    end
                end
            end
        end
    end
    scan('blue', env.mission.coalition.blue)
    scan('red', env.mission.coalition.red)
    return { callsigns = out }
end

local op = req.op
if op == 'status' then
    return envelope(true, snapshot())
elseif op == 'parking' then
    -- Parking spots for an airbase (by name). DCS exposes no taxi-route names,
    -- but each spot has a terminal index and distance to the runway, which we
    -- use to pick a taxi route per parking area.
    local out = { spots = {} }
    for _, b in ipairs(coalition.getAirbases(2)) do
        if b:getName() == req.airbase then
            local ok, parking = pcall(function() return b:getParking() end)
            if ok and parking then
                for _, p in ipairs(parking) do
                    local lat, lon, alt = coord.LOtoLL(p.vTerminalPos)
                    out.spots[#out.spots + 1] = {
                        term = p.Term_Index,
                        dist_rwy = p.fDistToRW,
                        lat = lat, lon = lon, alt = alt,
                    }
                end
            end
        end
    end
    return envelope(true, out)
elseif op == 'callsigns' then
    return envelope(true, callsigns())
elseif op == 'runways' then
    -- Runway ends for an airbase (by name): each has a name (e.g. "25"), the
    -- threshold position and the runway heading. Used to seed airspace.json.
    local out = { runways = {} }
    for _, b in ipairs(coalition.getAirbases(2)) do
        if b:getName() == req.airbase then
            local ok, rwys = pcall(function() return b:getRunways() end)
            if ok and rwys then
                for _, r in ipairs(rwys) do
                    local lat, lon, alt = coord.LOtoLL(r.position)
                    out.runways[#out.runways + 1] = {
                        name = tostring(r.Name or r.name or ''),
                        lat = lat, lon = lon, alt = alt,
                        course = r.course,
                        length = r.length,
                        width = r.width,
                    }
                end
            end
        end
    end
    return envelope(true, out)elseif op == 'tower' then
    -- Dispatcher/tower position for an airbase (by name), so the bot knows
    -- where the tower actually is without a hand-entered coordinate. Falls
    -- back to the tech object (control tower) then the airbase centre.
    for _, b in ipairs(coalition.getAirbases(2)) do
        if b:getName() == req.airbase then
            local p = nil
            local ok, disp = pcall(function() return b:getDispatcherTowerPos() end)
            if ok and disp then p = disp end
            if not p then
                local ok2, tech = pcall(function() return b:getTechObjectPos() end)
                if ok2 and tech then p = tech end
            end
            if not p then p = b:getPoint() end
            local lat, lon, alt = coord.LOtoLL(p)
            return envelope(true, { name = b:getName(), lat = lat, lon = lon, alt = alt })
        end
    end
    return envelope(false, 'NO_AIRBASE')
elseif op == 'weather' then
    local w = env.mission.weather or {}
    local wind = w.wind or {}
    local ground = wind.atGround or {}
    local clouds = w.clouds or {}
    local vis = w.visibility or {}
    local season = w.season or {}
    -- Mission time of day in seconds since midnight (for the ATIS letter).
    local mission_time_s = nil
    if timer and timer.getAbsTime then
        mission_time_s = timer.getAbsTime()
    elseif env.mission.start_time then
        mission_time_s = env.mission.start_time
    end
    return envelope(true, {
        qnh_mmhg = w.qnh,
        wind_dir = ground.dir,
        wind_speed_ms = ground.speed,
        wind_dir_2000 = (wind.at2000 or {}).dir,
        wind_speed_2000_ms = (wind.at2000 or {}).speed,
        clouds_base_m = clouds.base,
        clouds_density = clouds.density,
        clouds_preset = clouds.preset,
        visibility_m = vis.distance,
        temperature_c = season.temperature,
        fog = w.enable_fog,
        dust = w.enable_dust,
        mission_time_s = mission_time_s,
    })
elseif op == 'set_route' then
    local g = Group.getByName(req.group_name)
    if not g then return envelope(false, 'NO_GROUP') end
    g:getController():setTask({ id = 'Mission', params = { route = req.route_data } })
    return envelope(true, { moved = req.group_name })
elseif op == 'set_task' then
    local g = Group.getByName(req.group_name)
    if not g then return envelope(false, 'NO_GROUP') end
    g:getController():setTask(req.task_data)
    return envelope(true, { tasked = req.group_name })
else
    return envelope(false, 'UNKNOWN_OP')
end
