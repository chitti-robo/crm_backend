import sqlite3

IDS = (3, 4)  # test leads only
c = sqlite3.connect("dev.db")
ph = ",".join("?" * len(IDS))
for table in ("lead_sources", "lead_interests"):
    print(table, c.execute(f"delete from {table} where lead_id in ({ph})", IDS).rowcount)
print("leads", c.execute(f"delete from leads where id in ({ph})", IDS).rowcount)
c.commit()
c.close()