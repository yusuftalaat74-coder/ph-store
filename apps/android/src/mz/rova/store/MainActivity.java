package mz.rova.store;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.provider.MediaStore;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import java.io.File;

/**
 * The PH Store shell: a WebView on file:///android_asset/index.html.
 *
 * v4.3 adds the one thing the v2.0–v4.2 shell lacked: a WebChromeClient with
 * onShowFileChooser. Without it an {@code <input type="file">} in the page
 * was a dead button, and a pharmacy could not send the photo of its Alvará
 * from the phone at sign-up. The chooser offers the camera and the gallery /
 * files (images and PDF) in one list.
 *
 * The camera writes into this app's cache through {@link Files}, a minimal
 * content provider, because a file:// URI handed to another app throws on
 * Android 7+. No CAMERA permission is declared on purpose: an app that
 * declares it and has not been granted it is refused ACTION_IMAGE_CAPTURE,
 * while an app that does not declare it may use the camera app freely.
 *
 * The page learns that files can be picked from " PHStoreShell/4.3 files"
 * at the end of the user agent.
 */
public class MainActivity extends Activity {
    private static final int PICK = 43;

    private WebView web;
    private ValueCallback<Uri[]> pending;
    private Uri cameraUri;
    private File cameraFile;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        web = new WebView(this);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        s.setBuiltInZoomControls(false);
        s.setUserAgentString(s.getUserAgentString() + " PHStoreShell/4.3 files");
        web.setWebViewClient(new WebViewClient());
        web.setWebChromeClient(new Chooser());
        setContentView(web);
        web.loadUrl("file:///android_asset/index.html");
    }

    private final class Chooser extends WebChromeClient {
        @Override
        public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                         FileChooserParams params) {
            if (pending != null) pending.onReceiveValue(null);
            pending = callback;

            Intent files = new Intent(Intent.ACTION_GET_CONTENT);
            files.addCategory(Intent.CATEGORY_OPENABLE);
            files.setType("*/*");
            files.putExtra(Intent.EXTRA_MIME_TYPES, new String[] {"image/*", "application/pdf"});

            Intent chooser = Intent.createChooser(files, null);
            Intent camera = cameraIntent();
            if (camera != null) chooser.putExtra(Intent.EXTRA_INITIAL_INTENTS, new Intent[] {camera});
            try {
                startActivityForResult(chooser, PICK);
            } catch (ActivityNotFoundException e) {
                pending = null;
                callback.onReceiveValue(null);
                return false;
            }
            return true;
        }
    }

    private Intent cameraIntent() {
        Intent camera = new Intent(MediaStore.ACTION_IMAGE_CAPTURE);
        if (camera.resolveActivity(getPackageManager()) == null) return null;
        Files.sweep(this);
        String name = "alvara-" + System.currentTimeMillis() + ".jpg";
        cameraFile = Files.fileFor(this, name);
        cameraUri = Files.uriFor(name);
        camera.putExtra(MediaStore.EXTRA_OUTPUT, cameraUri);
        camera.setClipData(ClipData.newRawUri("", cameraUri));
        camera.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION | Intent.FLAG_GRANT_READ_URI_PERMISSION);
        return camera;
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode != PICK || pending == null) {
            super.onActivityResult(requestCode, resultCode, data);
            return;
        }
        Uri[] result = null;
        if (resultCode == RESULT_OK) {
            result = WebChromeClient.FileChooserParams.parseResult(resultCode, data);
            // the camera answers with no data at all, having written the
            // photo where EXTRA_OUTPUT pointed
            if (result == null && cameraFile != null && cameraFile.length() > 0) {
                result = new Uri[] {cameraUri};
            }
        }
        pending.onReceiveValue(result);
        pending = null;
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }
}
