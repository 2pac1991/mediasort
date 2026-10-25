package de.mediasort.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.content.pm.PackageManager;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

/** Mediasort als App: zeigt die Weboberfläche des Servers, nimmt geteilte Links an und hilft bei Tailscale. */
public class MainActivity extends Activity {
    static final String DEFAULT_URL = "https://mediasort.tail780db.ts.net";
    static final String TAILSCALE = "com.tailscale.ipn";
    // Linkschutz-Seiten öffnen im eingebauten Browser, dort wird Click'n'Load direkt abgefangen
    static final java.util.regex.Pattern PROTECTORS = java.util.regex.Pattern.compile(
            "(^|\\.)(filecrypt\\.(cc|co|to)|linkcrypt\\.ws|keeplinks\\.(org|co)|safelinking\\.net|ncrypt\\.in|share-links\\.biz|hide\\.cx|links\\.snahp\\.it|relink\\.(us|to)|dir\\.cr|protect-links\\.net|linksafe\\.cc)$",
            java.util.regex.Pattern.CASE_INSENSITIVE);
    static final java.util.regex.Pattern URL_RE = java.util.regex.Pattern.compile("https?://[^\\s<>\"'`]+", java.util.regex.Pattern.CASE_INSENSITIVE);

    static boolean isProtector(Uri u) {
        return u != null && u.getHost() != null && PROTECTORS.matcher(u.getHost()).find();
    }

    /** Erste geteilte Schutzseite, aber nicht filecrypts Hilfsfenster (das enthält keine Links). */
    static String protectorPage(String text) {
        java.util.regex.Matcher m = URL_RE.matcher(text);
        while (m.find()) {
            Uri u = Uri.parse(m.group());
            String path = u.getPath() == null ? "" : u.getPath();
            if (isProtector(u) && !path.contains("helper.htm")) return m.group();
        }
        return null;
    }

    private WebView web;
    private View errorView;
    private SharedPreferences prefs;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("mediasort", MODE_PRIVATE);

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#0A0D12"));
        web = new WebView(this);
        root.addView(web, new FrameLayout.LayoutParams(-1, -1));
        errorView = buildErrorView();
        errorView.setVisibility(View.GONE);
        root.addView(errorView, new FrameLayout.LayoutParams(-1, -1));
        setContentView(root);

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setUserAgentString(s.getUserAgentString() + " MediasortApp/1.0");
        CookieManager.getInstance().setAcceptCookie(true);
        web.setBackgroundColor(Color.parseColor("#0A0D12"));
        web.setWebChromeClient(new WebChromeClient()); // alert/confirm der Seite als normale Dialoge
        web.setWebViewClient(new Client());
        // Downloads (z. B. eine neue APK) übernimmt der Browser
        web.setDownloadListener((url, ua, cd, mime, len) -> openExternal(Uri.parse(url)));

        if (state != null) web.restoreState(state);
        else handleIntent(getIntent());

        // Echtes Click'n'Load: Hintergrunddienst auf Port 9666 (Hinweis-Benachrichtigung braucht ab Android 13 die Erlaubnis)
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{android.Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
        CnlService.start(this);
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleIntent(intent);
    }

    @Override
    protected void onPause() {
        super.onPause();
        CookieManager.getInstance().flush(); // Anmeldung behalten
    }

    @SuppressWarnings("deprecation")
    @Override
    public void onBackPressed() {
        // Zurück schließt erst offene Fenster in Mediasort (die nutzen den Verlauf), dann die App
        if (errorView.getVisibility() != View.VISIBLE && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }

    static String baseUrl(android.content.Context ctx) {
        String u = ctx.getSharedPreferences("mediasort", MODE_PRIVATE).getString("url", DEFAULT_URL).trim();
        while (u.endsWith("/")) u = u.substring(0, u.length() - 1);
        return u;
    }

    private String base() {
        return baseUrl(this);
    }

    /** Geteilte Links landen direkt im Fenster "Links senden". */
    private void handleIntent(Intent intent) {
        String url = base() + "/";
        String path = intent != null ? intent.getStringExtra("path") : null;
        if (path != null && path.startsWith("/")) url = base() + path; // aus einer Benachrichtigung
        if (intent != null && Intent.ACTION_SEND.equals(intent.getAction())) {
            String text = intent.getStringExtra(Intent.EXTRA_TEXT);
            String subject = intent.getStringExtra(Intent.EXTRA_SUBJECT);
            String shared = ((subject != null ? subject + "\n" : "") + (text != null ? text : "")).trim();
            String page = shared.isEmpty() ? null : protectorPage(shared);
            if (page != null) {
                // Schutzseite gleich im eingebauten Browser öffnen: Captcha lösen, Click'n'Load tippen
                load(url);
                CnlBrowserActivity.open(this, page);
                return;
            }
            if (!shared.isEmpty()) url = base() + "/share?text=" + Uri.encode(shared);
        }
        load(url);
    }

    private void load(String url) {
        errorView.setVisibility(View.GONE);
        web.setVisibility(View.VISIBLE);
        web.loadUrl(url);
    }

    private boolean isOwn(Uri u) {
        Uri b = Uri.parse(base());
        return u.getHost() != null && u.getHost().equalsIgnoreCase(b.getHost());
    }

    private void openExternal(Uri u) {
        try {
            if ("intent".equals(u.getScheme())) {
                startActivity(Intent.parseUri(u.toString(), Intent.URI_INTENT_SCHEME));
            } else {
                startActivity(new Intent(Intent.ACTION_VIEW, u));
            }
        } catch (Exception e) {
            // keine passende App installiert
        }
    }

    private void openTailscale() {
        Intent i = getPackageManager().getLaunchIntentForPackage(TAILSCALE);
        if (i != null) {
            startActivity(i);
            return;
        }
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse("market://details?id=" + TAILSCALE)));
        } catch (ActivityNotFoundException e) {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse("https://play.google.com/store/apps/details?id=" + TAILSCALE)));
        }
    }

    private void askAddress() {
        EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        input.setText(base());
        input.setSelectAllOnFocus(true);
        int pad = dp(20);
        FrameLayout box = new FrameLayout(this);
        box.setPadding(pad, dp(8), pad, 0);
        box.addView(input);
        new AlertDialog.Builder(this)
                .setTitle("Adresse von Mediasort")
                .setMessage("Zum Beispiel " + DEFAULT_URL)
                .setView(box)
                .setPositiveButton("Speichern", (d, w) -> {
                    String u = input.getText().toString().trim();
                    if (!u.isEmpty()) {
                        if (!u.startsWith("http://") && !u.startsWith("https://")) u = "https://" + u;
                        prefs.edit().putString("url", u).apply();
                        load(base() + "/");
                    }
                })
                .setNegativeButton("Abbrechen", null)
                .show();
    }

    private class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            Uri u = request.getUrl();
            String scheme = u.getScheme() == null ? "" : u.getScheme();
            if ((scheme.equals("http") || scheme.equals("https")) && isOwn(u)) return false;
            if (isProtector(u) && !(u.getPath() != null && u.getPath().contains("helper.htm"))) {
                CnlBrowserActivity.open(MainActivity.this, u.toString());
                return true;
            }
            // Fremde Seiten (Download-Seiten, Home Assistant, Tailscale ...) im Browser bzw. in der passenden App
            openExternal(u);
            return true;
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (request.isForMainFrame()) {
                web.setVisibility(View.INVISIBLE);
                errorView.setVisibility(View.VISIBLE);
            }
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            CookieManager.getInstance().flush();
        }
    }

    // ---------- Hinweis, wenn der Server nicht erreichbar ist ----------
    private int dp(int v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, getResources().getDisplayMetrics());
    }

    private Button button(String text, boolean primary, View.OnClickListener click) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextSize(16);
        b.setTextColor(Color.parseColor(primary ? "#1D1405" : "#F3F5F8"));
        GradientDrawable bg = new GradientDrawable();
        bg.setCornerRadius(dp(14));
        bg.setColor(Color.parseColor(primary ? "#F5B83D" : "#1A222D"));
        if (!primary) bg.setStroke(dp(1), Color.parseColor("#2A3441"));
        b.setBackground(bg);
        b.setOnClickListener(click);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-1, dp(52));
        lp.topMargin = dp(12);
        b.setLayoutParams(lp);
        return b;
    }

    private View buildErrorView() {
        LinearLayout v = new LinearLayout(this);
        v.setOrientation(LinearLayout.VERTICAL);
        v.setGravity(Gravity.CENTER);
        v.setPadding(dp(28), dp(28), dp(28), dp(28));
        v.setBackgroundColor(Color.parseColor("#0A0D12"));

        TextView icon = new TextView(this);
        icon.setText("🔒");
        icon.setTextSize(44);
        icon.setGravity(Gravity.CENTER);
        v.addView(icon, new LinearLayout.LayoutParams(-1, ViewGroup.LayoutParams.WRAP_CONTENT));

        TextView title = new TextView(this);
        title.setText("Mediasort nicht erreichbar");
        title.setTextSize(22);
        title.setTextColor(Color.parseColor("#F3F5F8"));
        title.setGravity(Gravity.CENTER);
        title.setPadding(0, dp(12), 0, dp(8));
        title.setTypeface(title.getTypeface(), android.graphics.Typeface.BOLD);
        v.addView(title, new LinearLayout.LayoutParams(-1, ViewGroup.LayoutParams.WRAP_CONTENT));

        TextView text = new TextView(this);
        text.setText("Ist Tailscale verbunden? Öffne die Tailscale-App, schalte die Verbindung ein und versuche es dann erneut.");
        text.setTextSize(15);
        text.setTextColor(Color.parseColor("#8A94A3"));
        text.setGravity(Gravity.CENTER);
        text.setLineSpacing(0, 1.25f);
        v.addView(text, new LinearLayout.LayoutParams(-1, ViewGroup.LayoutParams.WRAP_CONTENT));

        v.addView(button("Tailscale öffnen", true, x -> openTailscale()));
        v.addView(button("Erneut versuchen", false, x -> load(base() + "/")));
        v.addView(button("Adresse ändern", false, x -> askAddress()));
        return v;
    }
}
