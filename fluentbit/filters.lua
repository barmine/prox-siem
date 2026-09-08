-- Reshapes raw Fluent Bit records into the same document shape Phase 1's
-- ingestion API already writes: severity, title, message, hostname, unit,
-- source_type as top-level fields, everything else nested under `fields`.
-- Kept in Lua (rather than a chain of modify/nest filters) so the mapping
-- logic lives in one place and is easy to unit-test by eye.

local function priority_to_severity(pri)
  local p = tonumber(pri)
  if p == nil then return "unknown" end
  if p <= 3 then return "error"
  elseif p == 4 then return "warning"
  elseif p == 5 then return "notice"
  else return "info"
  end
end

local function http_status_to_severity(status)
  local s = tonumber(status)
  if s == nil then return "unknown" end
  if s >= 500 then return "error"
  elseif s >= 400 then return "warning"
  else return "info"
  end
end

local function task_status_to_severity(status)
  if status == nil then return "unknown" end
  if status == "OK" then return "info" end
  return "error"
end

-- systemd journal entries (pve.journal / pbs.journal tags)
function journal_to_event(tag, timestamp, record)
  local unit = record["_SYSTEMD_UNIT"] or record["SYSLOG_IDENTIFIER"] or record["_TRANSPORT"] or "unknown"
  local hostname = record["_HOSTNAME"] or "unknown"
  local message = record["MESSAGE"] or ""
  local severity = priority_to_severity(record["PRIORITY"])

  local skip = {
    MESSAGE = true, PRIORITY = true, _SYSTEMD_UNIT = true, _HOSTNAME = true,
  }
  local extra = {}
  for k, v in pairs(record) do
    if not skip[k] then extra[k] = v end
  end

  local new_record = {
    severity = severity,
    title = "",
    message = message,
    hostname = hostname,
    unit = unit,
    source_type = "journal",
    fields = extra,
  }
  return 1, timestamp, new_record
end

-- PVE /var/log/pve/tasks/index and PBS /var/log/proxmox-backup/tasks/active
-- (already parsed by pve_task_index / pbs_task_active regex parsers).
-- `hostname` is pre-set by a `modify` filter before this runs.
function task_index_to_event(tag, timestamp, record)
  local status = record["status"]
  local severity = task_status_to_severity(status)
  local worker_type = record["worker_type"] or "unknown"

  local skip = {
    hostname = true, status = true, worker_type = true,
  }
  local extra = {}
  for k, v in pairs(record) do
    if not skip[k] then extra[k] = v end
  end

  local new_record = {
    severity = severity,
    title = worker_type,
    message = status,
    hostname = record["hostname"] or "unknown",
    unit = worker_type,
    source_type = "task-log",
    fields = extra,
  }
  return 1, timestamp, new_record
end

-- pveproxy /var/log/pveproxy/access.log (parsed by pve_access_log).
-- `hostname` is pre-set by a `modify` filter before this runs.
function access_log_to_event(tag, timestamp, record)
  local status = record["status"]
  local severity = http_status_to_severity(status)
  local method = record["method"] or ""
  local path = record["path"] or ""

  local skip = {
    hostname = true, status = true, method = true, path = true, time = true,
  }
  local extra = {}
  for k, v in pairs(record) do
    if not skip[k] then extra[k] = v end
  end

  local new_record = {
    severity = severity,
    title = method,
    message = method .. " " .. path .. " -> " .. tostring(status),
    hostname = record["hostname"] or "unknown",
    unit = "pveproxy-access",
    source_type = "access-log",
    fields = extra,
  }
  return 1, timestamp, new_record
end
