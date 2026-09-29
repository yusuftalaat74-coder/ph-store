package mz.rova.store;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;

import java.io.File;
import java.io.FileNotFoundException;

/**
 * Where the camera app writes the photo of the Alvará: one flat directory
 * in this app's cache, reachable only through a URI this app grants for the
 * one capture (exported=false, grantUriPermissions=true). The same URI is
 * what the WebView then reads to upload.
 *
 * content://mz.rova.store.files/shots/alvara-1727640000000.jpg
 */
public class Files extends ContentProvider {
    static final String AUTHORITY = "mz.rova.store.files";
    private static final long DAY_MS = 24L * 60 * 60 * 1000;

    static Uri uriFor(String name) {
        return Uri.parse("content://" + AUTHORITY + "/shots/" + name);
    }

    static File fileFor(Context c, String name) {
        File dir = new File(c.getCacheDir(), "shots");
        dir.mkdirs();
        return new File(dir, name);
    }

    /** Photos older than a day have been uploaded or abandoned. */
    static void sweep(Context c) {
        File[] old = new File(c.getCacheDir(), "shots").listFiles();
        if (old == null) return;
        long cutoff = System.currentTimeMillis() - DAY_MS;
        for (File f : old) if (f.lastModified() < cutoff) f.delete();
    }

    private File resolve(Uri uri) throws FileNotFoundException {
        java.util.List<String> seg = uri.getPathSegments();
        if (seg.size() != 2 || !"shots".equals(seg.get(0))) throw new FileNotFoundException(uri.toString());
        String name = seg.get(1);
        if (name.contains("/") || name.contains("..")) throw new FileNotFoundException(uri.toString());
        return fileFor(getContext(), name);
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        return ParcelFileDescriptor.open(resolve(uri), ParcelFileDescriptor.parseMode(mode));
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection, String[] args, String sort) {
        File f;
        try {
            f = resolve(uri);
        } catch (FileNotFoundException e) {
            return null;
        }
        MatrixCursor c = new MatrixCursor(new String[] {OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE});
        c.addRow(new Object[] {f.getName(), f.length()});
        return c;
    }

    @Override
    public String getType(Uri uri) {
        return "image/jpeg";
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String selection, String[] args) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] args) {
        return 0;
    }
}
