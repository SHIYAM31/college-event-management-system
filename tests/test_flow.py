import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.mkdtemp()
os.environ["EVENTHUB_DB"] = os.path.join(_tmp, "test.db")

import app as eventhub  # noqa: E402

FMT = "%Y-%m-%dT%H:%M"


def csrf(client):
    client.get("/login")
    with client.session_transaction() as s:
        return s["csrf"]


def login(client, email, password):
    return client.post("/login", data={"email": email, "password": password, "csrf_token": csrf(client)},
                       follow_redirects=True)


def post(client, url, **data):
    with client.session_transaction() as s:
        data["csrf_token"] = s.get("csrf", "")
    return client.post(url, data=data, follow_redirects=True)


class FlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        eventhub.app.config["TESTING"] = True
        cls.admin, cls.org, cls.stu, cls.stu2 = (eventhub.app.test_client() for _ in range(4))
        login(cls.admin, "admin@college.edu", "admin123")
        login(cls.org, "organizer@college.edu", "org123")
        for c, n in ((cls.stu, "a"), (cls.stu2, "b")):
            c.get("/signup")
            post(c, "/signup", name=f"Student {n}", email=f"{n}@x.edu", password="secret1",
                 department="CSE" if n == "a" else "IT", year="3")
            login(c, f"{n}@x.edu", "secret1")

    def new_event(self, capacity=1, deadline_hours=24, hours_ahead=48):
        now = datetime.now()
        post(self.org, "/organizer/events/new", title="AI Workshop", type="Workshop", description="d",
             event_date=(now + timedelta(hours=hours_ahead)).strftime(FMT), venue="Hall",
             capacity=str(capacity), deadline=(now + timedelta(hours=deadline_hours)).strftime(FMT))
        with eventhub.app.app_context():
            return eventhub.query("SELECT id FROM events ORDER BY id DESC LIMIT 1", one=True)["id"]

    def approve(self, eid):
        post(self.admin, f"/admin/events/{eid}/approve")

    def token(self, eid, email):
        with eventhub.app.app_context():
            return eventhub.query("""SELECT r.qr_token t FROM registrations r JOIN users u ON u.id=r.user_id
                                     WHERE r.event_id=? AND u.email=?""", (eid, email), one=True)["t"]

    def test_full_lifecycle(self):
        eid = self.new_event(capacity=1)
        # pending events are hidden from students
        self.assertEqual(self.stu.get(f"/events/{eid}").status_code, 404)           # TC05
        self.approve(eid)
        r = post(self.stu, f"/events/{eid}/register")                               # TC01
        self.assertIn(b"Registered", r.data)
        r = post(self.stu, f"/events/{eid}/register")                               # TC02
        self.assertIn(b"Already registered", r.data)
        r = post(self.stu2, f"/events/{eid}/register")                              # TC03
        self.assertIn(b"Event full", r.data)
        tok = self.token(eid, "a@x.edu")
        self.assertGreaterEqual(len(tok), 22)

        def scan(client, token, event_id):
            with client.session_transaction() as s:
                h = {"X-CSRF-Token": s["csrf"]}
            return client.post("/attendance/scan", json={"token": token, "event_id": event_id}, headers=h)

        self.assertEqual(scan(self.org, tok, eid).get_json()["status"], "ok")      # TC06
        r = scan(self.org, tok, eid)                                                # TC07
        self.assertEqual((r.status_code, r.get_json()["status"]), (409, "duplicate"))
        eid2 = self.new_event(capacity=5)
        self.approve(eid2)
        self.assertEqual(scan(self.org, tok, eid2).status_code, 400)                # TC08
        self.assertEqual(self.stu.get("/admin").status_code, 403)                   # TC09
        self.assertEqual(scan(self.stu, tok, eid).status_code, 403)
        # feedback needs attendance and a started event; this event is in the future
        r = self.stu2.get(f"/events/{eid}/feedback", follow_redirects=True)         # TC10
        self.assertIn(b"Only students who attended", r.data)
        with eventhub.app.app_context():
            eventhub.get_db().execute("UPDATE events SET event_date=? WHERE id=?",
                                      ((datetime.now() - timedelta(hours=1)).strftime(FMT), eid))
            eventhub.get_db().commit()
        post(self.stu, f"/events/{eid}/feedback", rating="4", comment="good")
        csv_resp = self.org.get(f"/events/{eid}/report.csv")                        # TC11
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn(b"Present", csv_resp.data)
        with eventhub.app.app_context():
            s = eventhub.event_stats(eid)
        self.assertEqual((s["registered"], s["present"], s["attendance_rate"], s["avg_rating"]),
                         (1, 1, 100.0, 4.0))

    def test_deadline_and_csrf(self):
        eid = self.new_event(capacity=5, deadline_hours=-1, hours_ahead=5)
        self.approve(eid)
        self.assertIn(b"Registration closed", post(self.stu, f"/events/{eid}/register").data)  # TC04
        self.assertEqual(self.stu.post(f"/events/{eid}/register").status_code, 400)

    def test_pages_render(self):
        for c, urls in ((self.stu, ["/events", "/my/registrations"]),
                        (self.org, ["/organizer/events", "/organizer/events/new"]),
                        (self.admin, ["/admin", "/admin/users"])):
            for u in urls:
                self.assertEqual(c.get(u).status_code, 200, u)


if __name__ == "__main__":
    unittest.main()
