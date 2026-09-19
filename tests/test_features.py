import io
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from unittest.mock import Mock, patch

from PIL import Image
import app as core
import features


class FeatureFlows(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        core.app.config.update(TESTING=True, DATABASE=os.path.join(self.temp.name,"test.db"), UPLOAD_FOLDER=os.path.join(self.temp.name,"uploads"))
        core.init_db()
        core._cache.update(time=0,data=[],source="")
        self.sheet = patch.object(core,"_load_from_google_sheets",return_value=core._load_from_csv())
        self.image = patch.object(core,"wikipedia_image",return_value="https://example.com/test.jpg")
        self.sheet.start(); self.image.start()
        self.addCleanup(self.sheet.stop); self.addCleanup(self.image.stop); self.addCleanup(self.temp.cleanup)
        self.client = core.app.test_client()

    def token(self, client=None):
        client = client or self.client
        client.get("/login", follow_redirects=True)
        with client.session_transaction() as session:
            return session["csrf_token"]

    def post(self, path, data=None, client=None, **kwargs):
        client = client or self.client
        payload = dict(data or {})
        payload["csrf_token"] = self.token(client)
        return client.post(path,data=payload,**kwargs)

    def member(self, name="observer", client=None):
        client = client or self.client
        response = self.post("/register",dict(username=name,email=name+"@example.com",password="Testing-pass-123",confirm_password="Testing-pass-123"),client=client)
        self.assertEqual(response.status_code,302)
        with client.session_transaction() as session:
            return session["user_id"]

    def admin(self):
        self.member("reviewer")
        result = core.app.test_cli_runner().invoke(args=["promote-admin","reviewer"])
        self.assertEqual(result.exit_code,0,result.output)

    def dbrow(self, sql, params=()):
        with core.app.app_context():
            return core.get_db().execute(sql,params).fetchone()

    def observation(self, **changes):
        payload=dict(species_id="SP001",location="Chitwan region",observed_on="2026-01-02",notes="Tracks observed without approaching wildlife.",category="Tracks")
        payload.update(changes)
        return self.post("/observations/new",payload)

    def test_all_public_and_member_pages_and_language(self):
        with patch.object(features,"fetch_news",return_value={"items":[],"error":True}):
            for path in ("/","/learn","/compare","/compare?species=SP001&species=SP003","/quiz","/news","/organizations","/community","/analytics","/events","/species/SP001"):
                self.assertEqual(self.client.get(path).status_code,200,path)
        self.member()
        for path in ("/dashboard","/alerts","/achievements","/observations/new"):
            self.assertEqual(self.client.get(path).status_code,200,path)
        response=self.client.get("/language/ne?next=/learn",follow_redirects=True)
        self.assertIn("सिकौँ र जोगाऔँ".encode(),response.data)
        self.assertIn(b'lang="ne"',response.data)
        self.assertEqual(self.client.get("/language/en?next=//evil.example").location,"/")

    def test_quiz_scoring_idempotence_ownership_and_certificate(self):
        user=self.member()
        response=self.post("/quiz/start")
        path=response.location
        self.assertEqual(self.client.get(path).status_code,200)
        attempt=self.dbrow("SELECT * FROM quiz_attempts WHERE user_id=?",(user,))
        qs=json.loads(attempt["questions"])
        answers={"answer_"+str(i):str(q["answer"]) for i,q in enumerate(qs)}
        self.assertEqual(self.client.get(path+"/certificate").status_code,404)
        self.assertEqual(self.post(path,answers).status_code,302)
        self.assertIn(b'100', self.client.get(path).data)
        self.assertEqual(self.client.get(path+"/certificate").status_code,200)
        self.post(path,{"answer_0":"99"})
        self.assertEqual(self.dbrow("SELECT score FROM quiz_attempts WHERE id=?",(attempt["id"],))[0],len(qs))
        other=core.app.test_client();self.member("other",other)
        self.assertEqual(other.get(path).status_code,404)
        self.assertEqual(other.get(path+"/certificate").status_code,404)

    def test_upload_validation_pending_privacy_moderation_and_alert(self):
        owner=self.member()
        image=Image.new("RGB",(80,60),"green")
        exif=Image.Exif();exif[315]="Private camera owner"
        buf=io.BytesIO();image.save(buf,"JPEG",exif=exif);buf.seek(0)
        response=self.observation(photo=(buf,"proof.jpg"),share="1",public_location="1",latitude="27.42",longitude="84.49")
        self.assertEqual(response.status_code,302)
        obs=self.dbrow("SELECT * FROM observations WHERE user_id=?",(owner,))
        self.assertEqual(obs["status"],"pending");self.assertEqual(obs["latitude"],27)
        photo=f"/observations/{obs['id']}/photo"
        anonymous=core.app.test_client()
        self.assertEqual(anonymous.get(photo).status_code,404)
        with self.client.get(photo) as response:
            self.assertEqual(response.status_code,200)
            with Image.open(io.BytesIO(response.data)) as clean:
                self.assertEqual(len(clean.getexif()),0)
        follower=core.app.test_client();self.member("follower",follower)
        self.post("/species/SP001/favorite",client=follower)
        self.post("/logout");self.admin()
        self.assertEqual(self.post(f"/admin/observations/{obs['id']}",dict(status="approved",review_note="General area only")).status_code,302)
        with anonymous.get(photo) as response:
            self.assertEqual(response.status_code,200)
        self.assertIn(b"New reviewed observation",follower.get("/alerts").data)
        self.post(f"/admin/observations/{obs['id']}",dict(status="hidden"))
        self.assertEqual(anonymous.get(photo).status_code,404)

    def test_sensitive_locations_and_unapproved_notes_are_not_public(self):
        self.member()
        self.observation(species_id="SP004",location="Secret sensitive site",share="1",public_location="1",latitude="27.31",longitude="84.21")
        row=self.dbrow("SELECT * FROM observations")
        self.assertEqual(row["sensitive"],1)
        self.assertNotIn(b"Secret sensitive site",self.client.get("/species/SP004").data)
        self.post("/logout");self.admin()
        self.post(f"/admin/observations/{row['id']}",dict(status="approved"))
        html=self.client.get("/community").data
        self.assertNotIn(b"Secret sensitive site",html)
        self.assertIn(b"Sensitive location hidden",html)
        self.assertNotIn(b'id="community-map"',html)

    def test_bad_upload_and_csrf_and_nonadmin_cannot_change_data(self):
        self.member()
        result=self.observation(photo=(io.BytesIO(b"<script>evil</script>"),"image.jpg"))
        self.assertIn(b"safe image",result.data)
        self.assertEqual(self.dbrow("SELECT COUNT(*) FROM observations")[0],0)
        self.assertEqual(self.client.post("/learn/pledge",data={"action":"learn"}).status_code,400)
        for path in ("/admin","/admin/species/SP001/edit"):
            self.assertEqual(self.client.get(path).status_code,403)
        self.assertEqual(self.post("/refresh").status_code,403)
        self.assertEqual(self.post("/admin/users/1").status_code,403)

    def test_live_change_alerts_deduplicate_and_preferences_apply(self):
        uid=self.member()
        self.post("/species/SP001/favorite")
        with core.app.app_context():
            rows=core._load_from_csv()
            features.sync_changes(rows)
            rows[0]["IUCN_Status"]="Critically Endangered"
            features.sync_changes(rows);features.sync_changes(rows)
            self.assertEqual(core.get_db().execute("SELECT COUNT(*) FROM notifications WHERE user_id=?",(uid,)).fetchone()[0],1)
            core.get_db().execute("UPDATE users SET notify_species=0 WHERE id=?",(uid,));core.get_db().commit()
            rows[0]["Key_Fact"]="New profile fact"
            features.sync_changes(rows)
            self.assertEqual(core.get_db().execute("SELECT COUNT(*) FROM notifications WHERE user_id=?",(uid,)).fetchone()[0],1)

    def test_events_follow_and_reminder_and_admin_amendments(self):
        self.admin()
        response=self.post("/admin/events",dict(title="Local forest walk",event_date=(date.today()+timedelta(days=2)).isoformat(),location="Community center",description="Guided conservation learning.",url="https://example.com/event"))
        self.assertEqual(response.status_code,302)
        eid=self.dbrow("SELECT id FROM events")[0]
        self.post("/events",dict(event_id=eid))
        self.assertIn(b"Upcoming: Local forest walk",self.client.get("/alerts").data)
        fields=dict(core._load_from_csv()[0]);fields["Common_Name"]="Tiger project note"
        self.post("/admin/species/SP001/edit",fields)
        self.assertIn(b"Tiger project note",self.client.get("/species/SP001").data)
        self.assertIn(b"project amendment",self.client.get("/species/SP001").data)
        self.post("/admin/species/SP001/reset")
        self.assertNotIn(b"Tiger project note",self.client.get("/species/SP001").data)
        self.assertEqual(self.client.get("/admin").status_code,200)

    def test_pledges_progress_and_user_owned_export(self):
        uid=self.member()
        for action in ("habitat","observe","learn"):
            self.post("/learn/pledge",dict(action=action))
        self.post("/learn/pledge",dict(action="learn"))
        self.assertEqual(self.dbrow("SELECT COUNT(*) FROM pledges WHERE user_id=?",(uid,))[0],3)
        self.observation(notes="=unsafe spreadsheet formula")
        export=self.client.get("/observations/export").data
        self.assertIn(b"'=unsafe spreadsheet formula",export)
        other=core.app.test_client();self.member("other",other)
        self.assertNotIn(b"unsafe spreadsheet formula",other.get("/observations/export?all=1").data)
        self.assertEqual(self.client.get("/achievements").status_code,200)

    def test_news_parser_handles_rss_and_failure_without_fake_news(self):
        features.news_cache.update(time=0,items=[],error=False)
        response=Mock(content=b'<rss><channel><item><title>Nepal forest restoration</title><link>https://example.com/story</link><pubDate>Fri, 18 Sep 2026 00:00:00 GMT</pubDate><category>Nepal</category></item></channel></rss>')
        with patch.object(features.requests,"get",return_value=response):
            page=self.client.get("/news?topic=Nepal")
            self.assertIn(b"Nepal forest restoration",page.data)
        features.news_cache.update(time=0)
        with patch.object(features.requests,"get",side_effect=features.requests.RequestException("offline")):
            self.assertIn(b"temporarily unavailable",self.client.get("/news").data)

    def test_legacy_database_migration_keeps_records_and_defaults_private(self):
        conn=sqlite3.connect(":memory:")
        conn.executescript("CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT); CREATE TABLE observations(id INTEGER PRIMARY KEY,user_id INTEGER,species_id TEXT,observed_on TEXT,location TEXT,notes TEXT); INSERT INTO users VALUES (9,'legacy'); INSERT INTO observations VALUES(4,9,'SP001','2026-01-01','General area','Old journal note');")
        features.migrate(conn);features.migrate(conn)
        self.assertEqual(conn.execute("SELECT username,is_admin FROM users").fetchone(),("legacy",0))
        self.assertEqual(conn.execute("SELECT notes,status FROM observations").fetchone(),("Old journal note","private"))
        conn.close()


if __name__=="__main__":
    unittest.main()
