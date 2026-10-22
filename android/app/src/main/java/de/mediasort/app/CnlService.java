package de.mediasort.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;
import android.webkit.CookieManager;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URL;
import java.net.URLDecoder;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Echtes Click'n'Load auf dem Handy: lauscht wie der JDownloader am PC auf 127.0.0.1:9666.
 * Tippt man im Browser auf einer Download-Seite auf "Click'n'Load", schickt die Seite die
 * verschlüsselten Links hierher; die App reicht sie an Mediasort weiter, Mediasort entschlüsselt
 * sie und gibt sie an den JDownloader.
 */
public class CnlService extends Service {
    static final String TAG = "MediasortCNL";
    static final int PORT = 9666;
    static final String CH_RUN = "cnl", CH_RESULT = "cnl_result";
    static final int ID_RUN = 1;

    private ServerSocket server;
    private final ExecutorService pool = Executors.newCachedThreadPool();
    private int resultId = 100;

    static void start(Context ctx) {
        Intent i = new Intent(ctx, CnlService.class);
        try {
            ctx.startForegroundService(i);
        } catch (Exception e) {
            Log.w(TAG, "Dienst nicht gestartet", e);
        }
    }

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(new NotificationChannel(CH_RUN, "Click'n'Load bereit", NotificationManager.IMPORTANCE_MIN));
        nm.createNotificationChannel(new NotificationChannel(CH_RESULT, "Click'n'Load Ergebnis", NotificationManager.IMPORTANCE_DEFAULT));
        Notification n = new Notification.Builder(this, CH_RUN)
                .setSmallIcon(R.drawable.ic_stat)
                .setContentTitle("Click'n'Load bereit")
                .setContentText("Im Browser auf Click'n'Load tippen, die Links gehen an den JDownloader.")
                .setContentIntent(openApp(""))
                .setOngoing(true)
                .build();
        if (Build.VERSION.SDK_INT >= 34) startForeground(ID_RUN, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(ID_RUN, n);
        pool.execute(this::listen);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        try {
            if (server != null) server.close();
        } catch (Exception ignored) {
        }
        pool.shutdownNow();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private PendingIntent openApp(String path) {
        Intent i = new Intent(this, MainActivity.class).setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        i.putExtra("path", path);
        return PendingIntent.getActivity(this, path.hashCode(), i, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
    }

    private void listen() {
        try {
            server = new ServerSocket(PORT, 16, InetAddress.getByName("127.0.0.1"));
            while (!server.isClosed()) {
                Socket s = server.accept();
                pool.execute(() -> handle(s));
            }
        } catch (Exception e) {
            Log.w(TAG, "Port 9666 nicht verfügbar", e);
        }
    }

    // ---------- Mini-Webserver wie im JDownloader ----------
    private void handle(Socket s) {
        try (Socket sock = s) {
            sock.setSoTimeout(15000);
            InputStream in = sock.getInputStream();
            String head = readHead(in);
            if (head == null) return;
            String[] lines = head.split("\r\n");
            String[] req = lines[0].split(" ");
            if (req.length < 2) return;
            String method = req[0], target = req[1];
            int len = 0;
            for (String l : lines) {
                int c = l.indexOf(':');
                if (c > 0 && l.substring(0, c).trim().equalsIgnoreCase("content-length")) {
                    len = Integer.parseInt(l.substring(c + 1).trim());
                }
            }
            byte[] body = new byte[Math.min(len, 2_000_000)];
            int got = 0;
            while (got < body.length) {
                int r = in.read(body, got, body.length - got);
                if (r < 0) break;
                got += r;
            }
            String path = target.contains("?") ? target.substring(0, target.indexOf('?')) : target;
            Map<String, String> form = parseForm(target.contains("?") ? target.substring(target.indexOf('?') + 1) : "");
            form.putAll(parseForm(new String(body, 0, got, StandardCharsets.UTF_8)));

            OutputStream out = sock.getOutputStream();
            if (method.equals("OPTIONS")) {
                respond(out, 204, "text/plain", "");
            } else if (path.endsWith("/jdcheck.js")) {
                // Seiten zeigen den Click'n'Load-Knopf nur, wenn ein JDownloader antwortet
                respond(out, 200, "text/javascript", "jdownloader=true;\nvar version='43307';\n");
            } else if (path.endsWith("/crossdomain.xml")) {
                respond(out, 200, "text/xml", "<?xml version=\"1.0\"?><cross-domain-policy><allow-access-from domain=\"*\" /></cross-domain-policy>");
            } else if (path.startsWith("/flash/add")) {
                respond(out, 200, "text/plain", "success\r\n");
                if (form.containsKey("crypted") || form.containsKey("urls")) pool.execute(() -> forward(form));
            } else if (path.equals("/flash") || path.equals("/flash/")) {
                respond(out, 200, "text/plain", "JDownloader\r\n");
            } else {
                respond(out, 200, "text/plain", "");
            }
        } catch (Exception e) {
            Log.w(TAG, "Anfrage fehlgeschlagen", e);
        }
    }

    private static String readHead(InputStream in) throws Exception {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        int b, state = 0;
        while ((b = in.read()) != -1) {
            buf.write(b);
            state = (b == '\r' && (state == 0 || state == 2)) ? state + 1 : (b == '\n' && (state == 1 || state == 3)) ? state + 1 : 0;
            if (state == 4) return buf.toString("ISO-8859-1");
            if (buf.size() > 65536) return null;
        }
        return null;
    }

    private static Map<String, String> parseForm(String s) {
        Map<String, String> m = new HashMap<>();
        for (String part : s.split("&")) {
            if (part.isEmpty()) continue;
            int eq = part.indexOf('=');
            try {
                String k = URLDecoder.decode(eq < 0 ? part : part.substring(0, eq), "UTF-8");
                String v = eq < 0 ? "" : URLDecoder.decode(part.substring(eq + 1), "UTF-8");
                m.put(k, v);
            } catch (Exception ignored) {
            }
        }
        return m;
    }

    private static void respond(OutputStream out, int code, String type, String body) throws Exception {
        byte[] b = body.getBytes(StandardCharsets.UTF_8);
        String head = "HTTP/1.1 " + code + (code == 200 ? " OK" : " No Content") + "\r\n"
                + "Content-Type: " + type + "; charset=utf-8\r\n"
                + "Content-Length: " + b.length + "\r\n"
                + "Access-Control-Allow-Origin: *\r\n"
                + "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
                + "Access-Control-Allow-Headers: *\r\n"
                + "Access-Control-Allow-Private-Network: true\r\n"
                + "Connection: close\r\n\r\n";
        out.write(head.getBytes(StandardCharsets.ISO_8859_1));
        out.write(b);
        out.flush();
    }

    // ---------- Weiter an Mediasort ----------
    private void forward(Map<String, String> form) {
        String base = MainActivity.baseUrl(this);
        try {
            JSONObject req = new JSONObject();
            for (String k : new String[]{"crypted", "jk", "urls", "package"}) {
                if (form.containsKey(k)) req.put(k, form.get(k));
            }
            JSONObject dec = post(base + "/api/cnl/decode", req);
            JSONArray urls = dec.getJSONArray("urls");
            StringBuilder text = new StringBuilder();
            for (int i = 0; i < urls.length(); i++) text.append(urls.getString(i)).append('\n');
            String pkg = dec.optString("package", "");
            JSONObject send = new JSONObject().put("text", text.toString()).put("package", pkg);
            JSONObject res = post(base + "/api/links", send);
            int n = res.optInt("count", urls.length());
            notifyResult("An JDownloader gesendet", (pkg.isEmpty() ? "" : pkg + " · ") + n + (n == 1 ? " Link" : " Links"), "/?jd=grabber");
        } catch (Exception e) {
            String msg = e.getMessage() == null ? e.toString() : e.getMessage();
            notifyResult("Click'n'Load fehlgeschlagen", msg, "/");
        }
    }

    private JSONObject post(String url, JSONObject body) throws Exception {
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
        int code = c.getResponseCode();
        InputStream in = code < 400 ? c.getInputStream() : c.getErrorStream();
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        if (in != null) {
            byte[] b = new byte[8192];
            int r;
            while ((r = in.read(b)) > 0) buf.write(b, 0, r);
        }
        JSONObject res = buf.size() > 0 ? new JSONObject(buf.toString("UTF-8")) : new JSONObject();
        if (code == 401) throw new Exception("Bitte erst in der Mediasort-App anmelden");
        if (code >= 400) throw new Exception(res.optString("error", "Fehler " + code));
        return res;
    }

    private void notifyResult(String title, String text, String path) {
        Notification n = new Notification.Builder(this, CH_RESULT)
                .setSmallIcon(R.drawable.ic_stat)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setContentIntent(openApp(path))
                .setAutoCancel(true)
                .build();
        getSystemService(NotificationManager.class).notify(resultId++, n);
    }
}
