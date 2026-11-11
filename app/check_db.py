import sqlite3

c = sqlite3.connect("dev.db")
print("leads:", c.execute(
    "select id, name, phone from leads where name like 'Test Student%' or phone like '%9876543210'"
).fetchall())
print("events by provider:", c.execute(
    "select provider, count(*) from integration_events group by provider"
).fetchall())
print("last lead_sources:", c.execute(
    "select * from lead_sources order by rowid desc limit 3"
).fetchall())
c.close()