# Logs

The Logs page (`?tab=logs`) shows the bridge's own log, stored in its database. Use it to
see what the bridge did and why: commands sent, schedules that fired, connection problems.
It is on by default.

![Logs page](../../images/ui/page-logs.png)

## Level chips

The chips at the top count entries per level in the current time span: **DEBUG**, **INFO**,
**WARNING**, **ERROR**, **CRITICAL**. A level with entries is tinted. Click a chip to show
only that level. Click it again to clear the filter.

## Filters

| Filter | Options | Default |
|---|---|---|
| Search messages… | Free text, searched as you type | empty |
| Level | All Levels, DEBUG, INFO, WARNING, ERROR, CRITICAL | All Levels |
| Gateway | All Gateways or one gateway (only with more than one gateway) | All Gateways |
| Source | The part of the bridge that wrote the entry (for example the poller, the MQTT publisher, the scheduler) | All Sources |
| Time span | Last 1h, 6h, 24h, 7 days, 30 days, All time, Custom… | Last 24h |
| Auto | Reloads every 5 seconds while the page is open | On |
| Refresh | Reloads now | — |

**Custom…** shows From → To boxes in the bridge's timezone; press **Apply**.

On a phone, everything except Search folds behind a **Filters** button, which shows how
many filters are active.

## The table

| Column | Meaning |
|---|---|
| Time | When it was logged, in the bridge's timezone. Hover for the full date. |
| Level | DEBUG to CRITICAL. |
| Source | Short name of the logger. Hover for the full name. |
| Message | The log message. |

Below the table: the number of entries, **Prev / Next** pages, and the page size
(50, **100**, 200, 500).

!!! tip "Reporting a problem"
    Don't paste raw logs into a GitHub issue. Use
    [Settings → Admin → Support info](settings-admin.md#support-info), which removes
    serials, addresses, names and tokens first.
