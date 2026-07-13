#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tests E2E de UI para TYM Music, con Playwright (navegador real).

Cubre flujos que un test de API no puede ver: verificar en un navegador de
verdad que el escape de innerHTML (esc()) neutraliza un payload de XSS en vez
de ejecutarlo, tanto en el panel del dueño (billetera/registro por celular)
como en el dashboard del TYM master.

Requiere: pip install -r requirements-dev.txt && playwright install chromium

Uso:  python3 tests/test_ui.py
"""
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(os.path.dirname(HERE), "app")
SERVER = os.path.join(APP_DIR, "server.py")


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def api_post(base, path, body):
    data = json.dumps(body).encode("utf-8")
    r = urllib.request.Request(base + path, data=data, method="POST",
                                headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(r, timeout=10)
        return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class UITestCase(unittest.TestCase):
    """Servidor real (subproceso, corriendo en un directorio temporal para
    nunca tocar app/data.json) + navegador Chromium real, por clase de test."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="tym_uitest_")
        shutil.copytree(APP_DIR, cls.tmp, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("data.json", "qr.png", "__pycache__"))
        cls.port = free_port()
        env = os.environ.copy()
        env["PORT"] = str(cls.port)
        env["UPSTASH_REDIS_REST_URL"] = ""
        env["UPSTASH_REDIS_REST_TOKEN"] = ""
        env["TYM_OWNER_BARDEMO_PASS"] = "tym1234"
        env["TYM_OWNER_LAZONA_PASS"] = "tym1234"
        env["TYM_OWNER_TYM_PASS"] = "tymmaster"
        cls.proc = subprocess.Popen(
            [sys.executable, "server.py"], cwd=cls.tmp, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls._wait_ready()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def _wait_ready(cls):
        for _ in range(100):
            if cls.proc.poll() is not None:
                raise RuntimeError("server.py terminó antes de tiempo")
            try:
                urllib.request.urlopen(cls.base + "/api/catalog", timeout=1)
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("el server no respondió a tiempo")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except Exception:
            cls.proc.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def new_page(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        return page

    def new_phone(self):
        return "300" + str(secrets.randbelow(9_000_000) + 1_000_000)


class TestClienteFlujo(UITestCase):
    def test_entra_con_pin_de_mesa(self):
        page = self.new_page()
        page.goto(f"{self.base}/?v=bardemo")
        page.fill("#pinInput", "1111")
        page.click("text=Entrar")
        page.wait_for_selector("#gate", state="hidden", timeout=5000)

    def test_pin_incorrecto_muestra_error(self):
        page = self.new_page()
        page.goto(f"{self.base}/?v=bardemo")
        page.fill("#pinInput", "0000")
        page.click("text=Entrar")
        page.wait_for_function("document.getElementById('pinErr').textContent.length > 0", timeout=5000)


class TestAdminFlujo(UITestCase):
    def test_login_incorrecto_muestra_error(self):
        page = self.new_page()
        page.goto(f"{self.base}/admin")
        page.wait_for_selector("#loginGate", state="visible", timeout=5000)
        page.fill("#lgUser", "bardemo")
        page.fill("#lgPass", "contraseña-mala")
        page.click("text=Entrar")
        page.wait_for_function("document.getElementById('lgErr').textContent.length > 0", timeout=5000)

    def test_login_correcto_entra_al_panel(self):
        page = self.new_page()
        page.goto(f"{self.base}/admin")
        page.wait_for_selector("#loginGate", state="visible", timeout=5000)
        page.fill("#lgUser", "lazona")
        page.fill("#lgPass", "tym1234")
        page.click("text=Entrar")
        page.wait_for_selector("#loginGate", state="hidden", timeout=5000)
        tag = page.text_content("#venueTag")
        self.assertIn("La Zona", tag)


class TestXSSRegression(UITestCase):
    """Verifica en un navegador REAL que el payload de ataque queda neutralizado
    (texto plano) y NUNCA se ejecuta como script, tanto en el panel del dueño
    (billetera/registro por celular) como en el dashboard del TYM master."""

    PAYLOAD = "<img src=x onerror=\"window.__xss_fired=true\">"       # cabe en venue_name (60)
    PAYLOAD_SHORT = "<img src=x onerror=xssFired()>"                  # cabe en name (40)

    def _login_owner_and_enable_prepaid(self, page):
        page.goto(f"{self.base}/admin")
        page.fill("#lgUser", "bardemo")
        page.fill("#lgPass", "tym1234")
        page.click("text=Entrar")
        page.wait_for_selector("#loginGate", state="hidden", timeout=5000)
        page.click("#tbtn-settings")
        page.wait_for_selector("#prepaid", state="visible", timeout=5000)
        if not page.is_checked("#prepaid"):
            page.check("#prepaid")
            page.click("text=Guardar ajustes")
            page.wait_for_selector(".toast.show", timeout=5000)

    def test_nombre_malicioso_no_se_ejecuta_en_admin(self):
        page = self.new_page()
        page.add_init_script("window.xssFired = () => { window.__xss_fired = true; }")
        self._login_owner_and_enable_prepaid(page)

        phone = self.new_phone()
        status, body = api_post(self.base, "/api/register",
                                 {"v": "bardemo", "name": self.PAYLOAD_SHORT, "phone": phone, "email": "x@x.com"})
        self.assertEqual(status, 200, body)

        page.click("#tbtn-tables")
        page.wait_for_selector("#custList", state="visible", timeout=5000)
        page.wait_for_function(
            "document.getElementById('custList').textContent.includes('img src')", timeout=5000)
        fired = page.evaluate("window.__xss_fired === true")
        self.assertFalse(fired, "el payload se EJECUTÓ como script — el fix de XSS falló")
        inner_html = page.eval_on_selector("#custList", "el => el.innerHTML")
        self.assertIn("&lt;img", inner_html, "el nombre debe quedar escapado (&lt;) en el HTML, no como tag real")

    def test_venue_name_malicioso_no_se_ejecuta_en_dashboard_tym(self):
        owner_page = self.new_page()
        self._login_owner_and_enable_prepaid(owner_page)
        owner_page.fill("#venue", self.PAYLOAD)
        owner_page.click("text=Guardar ajustes")
        owner_page.wait_for_selector(".toast.show", timeout=5000)

        master_page = self.new_page()
        master_page.add_init_script("window.xssFired = () => { window.__xss_fired = true; }")
        master_page.goto(f"{self.base}/tym")
        master_page.fill("#lgUser", "tym")
        master_page.fill("#lgPass", "tymmaster")
        master_page.click("text=Entrar")
        master_page.wait_for_selector("#loginGate", state="hidden", timeout=5000)
        master_page.wait_for_function(
            "document.getElementById('venueList').textContent.includes('img src')", timeout=5000)
        fired = master_page.evaluate("window.__xss_fired === true")
        self.assertFalse(fired, "el venue_name malicioso se EJECUTÓ en el dashboard del TYM master")
        inner_html = master_page.eval_on_selector("#venueList", "el => el.innerHTML")
        self.assertIn("&lt;img", inner_html, "el venue_name debe quedar escapado (&lt;), no como tag real")


if __name__ == "__main__":
    unittest.main(verbosity=2)
