package de.mediasort.app;

import android.content.Context;
import android.webkit.CookieManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/** Click'n'Load-Daten an Saugmeister: entschlüsseln lassen und an den JDownloader geben. */
final class CnlForwarder {
    static final class Result {
        final int count;
        final String pkg;

        Result(int count, String pkg) {
            this.count = count;
            this.pkg = pkg;
        }

        String text() {
            return (pkg.isEmpty() ? "" : pkg + " · ") + count + (count == 1 ? " Link" : " Links");
        }
    }

    private CnlForwarder() {
    }

    /** Erwartet die Felder eines CNL-Formulars (crypted, jk, urls, package) oder Kandidaten (keys, crypts). */
    static Result send(Context ctx, JSONObject form) throws Exception {
        String base = MainActivity.baseUrl(ctx);
        JSONObject req = new JSONObject();
        for (String k : new String[]{"crypted", "jk", "urls", "package", "keys", "crypts"}) {
            String v = form.optString(k, "");
            if (!v.isEmpty()) req.put(k, v);
        }
        JSONObject dec = post(base + "/api/cnl/decode", req);
        JSONArray urls = dec.getJSONArray("urls");
        StringBuilder text = new StringBuilder();
        for (int i = 0; i < urls.length(); i++) text.append(urls.getString(i)).append('\n');
        String pkg = dec.optString("package", "");
        JSONObject res = post(base + "/api/links", new JSONObject().put("text", text.toString()).put("package", pkg));
        return new Result(res.optInt("count", urls.length()), pkg);
    }

    static JSONObject post(String url, JSONObject body) throws Exception {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(15000);
        c.setReadTimeout(30000);
        c.setRequestMethod("POST");
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", "application/json");
        // Anmeldung der App (gleiches Cookie wie in der Oberfläche)
        String cookie = CookieManager.getInstance().getCookie(url);
        if (cookie != null) c.setRequestProperty("Cookie", cookie);
        try (OutputStream o = c.getOutputStream()) {
            o.write(body.toString().getBytes(StandardCharsets.UTF_8));
        }
        int code;
        try {
            code = c.getResponseCode();
        } catch (java.io.IOException e) {
            throw new Exception("Saugmeister nicht erreichbar – ist Tailscale verbunden?");
        }
        InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        if (in != null) {
            byte[] b = new byte[8192];
            int r;
            while ((r = in.read(b)) > 0) buf.write(b, 0, r);
        }
        JSONObject res;
        try {
            res = buf.size() > 0 ? new JSONObject(buf.toString("UTF-8")) : new JSONObject();
        } catch (Exception e) {
            res = new JSONObject();
        }
        if (code == 401) throw new Exception("Bitte erst in der Saugmeister-App anmelden");
        if (code >= 400) throw new Exception(res.optString("error", "Fehler " + code));
        return res;
    }
}
