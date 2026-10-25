package de.mediasort.app;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.Message;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.TextView;
import android.widget.Toast;

import androidx.webkit.WebViewCompat;
import androidx.webkit.WebViewFeature;

import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Eingebauter Browser für Linkschutz-Seiten (filecrypt & Co.). Captcha hier lösen, dann auf Click'n'Load
 * tippen: Die App fängt die Daten direkt in der Seite ab. So braucht es weder Port 9666 noch die
 * Erlaubnis von Chrome, auf das Gerät zuzugreifen.
 */
public class CnlBrowserActivity extends Activity {
    static final String EXTRA_URL = "url";

    private FrameLayout stack;
    private final List<WebView> views = new ArrayList<>();
    private TextView title;
    private ProgressBar progress;
    private String hook;
    private final ExecutorService pool = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());
    private String lastData = "";
    private long lastTime = 0;
    private boolean busy = false;

    static void open(Context ctx, String url) {
        Intent i = new Intent(ctx, CnlBrowserActivity.class).putExtra(EXTRA_URL, url);
        if (!(ctx instanceof Activity)) i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        ctx.startActivity(i);
    }

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        hook = readAsset("cnl_hook.js");

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(Color.parseColor("#0A0D12"));
        root.addView(buildBar());
        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setMax(100);
        root.addView(progress, new LinearLayout.LayoutParams(-1, dp(3)));
        stack = new FrameLayout(this);
        root.addView(stack, new LinearLayout.LayoutParams(-1, 0, 1));
        root.addView(buildHint());
        setContentView(root);

        WebView web = newWebView();
        stack.addView(web, new FrameLayout.LayoutParams(-1, -1));
        views.add(web);
        if (state != null) web.restoreState(state);
        else load(getIntent());
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        load(intent);
    }

    private void load(Intent intent) {
        String url = intent == null ? null : intent.getStringExtra(EXTRA_URL);
        if (url == null || url.isEmpty()) return;
        while (views.size() > 1) closeTop();
        views.get(0).loadUrl(url);
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        if (!views.isEmpty()) views.get(0).saveState(out);
    }

    @Override
    protected void onPause() {
        super.onPause();
        CookieManager.getInstance().flush();
    }

    @Override
    protected void onDestroy() {
        for (WebView w : views) w.destroy();
        pool.shutdownNow();
        super.onDestroy();
    }

    @SuppressWarnings("deprecation")
    @Override
    public void onBackPressed() {
        WebView top = top();
        if (views.size() > 1) closeTop();
        else if (top != null && top.canGoBack()) top.goBack();
        else super.onBackPressed();
    }

    private WebView top() {
        return views.isEmpty() ? null : views.get(views.size() - 1);
    }

    private void closeTop() {
        if (views.size() <= 1) return;
        WebView w = views.remove(views.size() - 1);
        stack.removeView(w);
        w.destroy();
    }

    // ---------- WebView ----------
    private WebView newWebView() {
        WebView w = new WebView(this);
        WebSettings s = w.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setSupportMultipleWindows(true); // filecrypt öffnet für Click'n'Load ein Hilfsfenster
        s.setJavaScriptCanOpenWindowsAutomatically(true);
        s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        // Wie ein normaler Chrome auftreten, manche Seiten sperren eingebettete Browser
        s.setUserAgentString(s.getUserAgentString().replace("; wv", "").replace(" Version/4.0", ""));
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(w, true);
        w.setBackgroundColor(Color.WHITE);
        w.addJavascriptInterface(new Bridge(), "MediasortCnl");
        boolean early = WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT);
        if (early) {
            WebViewCompat.addDocumentStartJavaScript(w, hook, Collections.singleton("*"));
        }
        w.setWebViewClient(new Client(!early));
        w.setWebChromeClient(new Chrome());
        return w;
    }

    private class Client extends WebViewClient {
        private final boolean lateHook;

        Client(boolean lateHook) {
            this.lateHook = lateHook;
        }

        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri u = request.getUrl();
            String scheme = u.getScheme() == null ? "" : u.getScheme();
            if (isLocalCnl(u)) return true; // Formular, das als Seite zum JDownloader springen will
            // Fremde Apps (intent:, market: ...) aus Werbung nicht öffnen
            return !(scheme.equals("http") || scheme.equals("https") || scheme.equals("about") || scheme.equals("blob") || scheme.equals("data"));
        }

        @Override
        public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
            if (lateHook) view.evaluateJavascript(hook, null);
            if (view == views.get(0)) title.setText(Uri.parse(url).getHost());
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            if (lateHook) view.evaluateJavascript(hook, null);
            CookieManager.getInstance().flush();
        }

        @Override
        public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
            Uri u = request.getUrl();
            if (!isLocalCnl(u)) return null;
            // Wie der JDownloader antworten (z. B. jdcheck.js, damit der Click'n'Load-Knopf erscheint)
            String path = u.getPath() == null ? "/" : u.getPath();
            String type = "text/plain", body = "";
            if (path.endsWith("/jdcheck.js")) {
                type = "text/javascript";
                body = "jdownloader=true;\nvar version='43307';\n";
            } else if (path.endsWith("/crossdomain.xml")) {
                type = "text/xml";
                body = "<?xml version=\"1.0\"?><cross-domain-policy><allow-access-from domain=\"*\" /></cross-domain-policy>";
            } else if (path.startsWith("/flash/add")) {
                body = "success\r\n";
                String crypted = u.getQueryParameter("crypted"), urls = u.getQueryParameter("urls");
                if (crypted != null || urls != null) {
                    try {
                        JSONObject o = new JSONObject();
                        for (String k : u.getQueryParameterNames()) o.put(k, u.getQueryParameter(k));
                        receive(o.toString());
                    } catch (Exception ignored) {
                    }
                }
            } else if (path.equals("/flash") || path.equals("/flash/")) {
                body = "JDownloader\r\n";
            }
            Map<String, String> h = new HashMap<>();
            h.put("Access-Control-Allow-Origin", "*");
            h.put("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
            h.put("Access-Control-Allow-Headers", "*");
            h.put("Access-Control-Allow-Private-Network", "true");
            InputStream in = new ByteArrayInputStream(body.getBytes(StandardCharsets.UTF_8));
            return new WebResourceResponse(type, "utf-8", 200, "OK", h, in);
        }
    }

    private class Chrome extends WebChromeClient {
        @Override
        public boolean onCreateWindow(WebView view, boolean isDialog, boolean userGesture, Message resultMsg) {
            WebView child = newWebView();
            stack.addView(child, new FrameLayout.LayoutParams(-1, -1));
            views.add(child);
            WebView.WebViewTransport t = (WebView.WebViewTransport) resultMsg.obj;
            t.setWebView(child);
            resultMsg.sendToTarget();
            return true;
        }

        @Override
        public void onCloseWindow(WebView window) {
            int i = views.indexOf(window);
            if (i > 0) {
                views.remove(i);
                stack.removeView(window);
                window.destroy();
            }
        }

        @Override
        public void onProgressChanged(WebView view, int p) {
            progress.setProgress(p);
            progress.setVisibility(p >= 100 ? View.INVISIBLE : View.VISIBLE);
        }
    }

    static boolean isLocalCnl(Uri u) {
        String host = u.getHost() == null ? "" : u.getHost();
        return u.getPort() == 9666 && (host.equals("127.0.0.1") || host.equals("localhost") || host.equals("[::1]") || host.equals("::1"));
    }

    // ---------- Daten an Mediasort ----------
    private class Bridge {
        @JavascriptInterface
        public void send(String json) {
            receive(json);
        }
    }

    private synchronized void receive(String json) {
        long now = System.currentTimeMillis();
        // Manche Seiten schicken doppelt (Formular + Skript): nur einmal senden
        if (json.equals(lastData) && now - lastTime < 15000) return;
        if (busy) return;
        lastData = json;
        lastTime = now;
        busy = true;
        ui.post(() -> Toast.makeText(this, "Click'n'Load empfangen, sende an JDownloader …", Toast.LENGTH_SHORT).show());
        pool.execute(() -> {
            try {
                CnlForwarder.Result r = CnlForwarder.send(this, new JSONObject(json));
                ui.post(() -> {
                    Toast.makeText(this, "An JDownloader gesendet: " + r.text(), Toast.LENGTH_LONG).show();
                    Intent i = new Intent(this, MainActivity.class).setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
                    i.putExtra("path", "/?jd=grabber");
                    startActivity(i);
                    finish();
                });
            } catch (Exception e) {
                String msg = e.getMessage() == null ? e.toString() : e.getMessage();
                ui.post(() -> Toast.makeText(this, "Click'n'Load fehlgeschlagen: " + msg, Toast.LENGTH_LONG).show());
            } finally {
                synchronized (this) {
                    busy = false;
                }
            }
        });
    }

    private void scan() {
        WebView w = top();
        if (w == null) return;
        w.evaluateJavascript(hook + ";window.__mediasortScan&&window.__mediasortScan()", r -> {
            if (r != null && r.contains("none")) {
                Toast.makeText(this, "Auf dieser Seite keine Click'n'Load-Daten gefunden. Erst Captcha lösen.", Toast.LENGTH_LONG).show();
            }
        });
    }

    // ---------- Oberfläche ----------
    private int dp(int v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics());
    }

    private View buildBar() {
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(8), dp(6), dp(8), dp(6));
        bar.setBackgroundColor(Color.parseColor("#0A0D12"));

        Button close = smallButton("✕", false);
        close.setOnClickListener(v -> finish());
        bar.addView(close, new LinearLayout.LayoutParams(dp(44), dp(40)));

        title = new TextView(this);
        title.setTextColor(Color.parseColor("#F3F5F8"));
        title.setTextSize(15);
        title.setSingleLine(true);
        title.setPadding(dp(10), 0, dp(10), 0);
        bar.addView(title, new LinearLayout.LayoutParams(0, -2, 1));

        Button grab = smallButton("Links holen", true);
        grab.setOnClickListener(v -> scan());
        bar.addView(grab, new LinearLayout.LayoutParams(-2, dp(40)));
        return bar;
    }

    private View buildHint() {
        TextView t = new TextView(this);
        t.setText("Captcha lösen, dann auf Click'n'Load tippen – die Links gehen direkt an den JDownloader.");
        t.setTextColor(Color.parseColor("#8A94A3"));
        t.setTextSize(13);
        t.setGravity(Gravity.CENTER);
        t.setPadding(dp(14), dp(8), dp(14), dp(10));
        t.setBackgroundColor(Color.parseColor("#0A0D12"));
        return t;
    }

    private Button smallButton(String text, boolean primary) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextSize(14);
        b.setPadding(dp(12), 0, dp(12), 0);
        b.setMinWidth(0);
        b.setMinimumWidth(0);
        b.setTextColor(Color.parseColor(primary ? "#1D1405" : "#F3F5F8"));
        GradientDrawable bg = new GradientDrawable();
        bg.setCornerRadius(dp(12));
        bg.setColor(Color.parseColor(primary ? "#F5B83D" : "#1A222D"));
        b.setBackground(bg);
        return b;
    }

    private String readAsset(String name) {
        try (InputStream in = getAssets().open(name)) {
            java.io.ByteArrayOutputStream buf = new java.io.ByteArrayOutputStream();
            byte[] b = new byte[8192];
            int r;
            while ((r = in.read(b)) > 0) buf.write(b, 0, r);
            return buf.toString("UTF-8");
        } catch (Exception e) {
            return "";
        }
    }
}
