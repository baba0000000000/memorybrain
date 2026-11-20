package com.example.memorycapture;

import android.content.ContentResolver;
import android.content.ContentUris;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.provider.MediaStore;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.OutputStream;

public final class MediaStorePublisher {
    // Dossier de publication, réglable à la compilation dans
    // android/gradle.properties (clé `memorybrain.audioDir`). Aucun chemin
    // n'est figé ici : la valeur vient de la configuration du build.
    private static final String RELATIVE_PATH = BuildConfig.AUDIO_RELATIVE_PATH;
    private static final String MIME_TYPE = "audio/wav";

    private MediaStorePublisher() {}

    public static void publishReadyFile(ContentResolver resolver, File file) {
        if (!file.exists() || !file.getName().endsWith(".wav")) {
            return;
        }

        // Idempotence: if the final item already exists, only the private copy is cleaned up.
        if (findPublishedItem(resolver, file.getName()) != null) {
            file.delete();
            return;
        }

        ContentValues values = new ContentValues();
        values.put(MediaStore.Audio.Media.DISPLAY_NAME, file.getName());
        values.put(MediaStore.Audio.Media.MIME_TYPE, MIME_TYPE);
        values.put(MediaStore.Audio.Media.RELATIVE_PATH, RELATIVE_PATH);
        if (Build.VERSION.SDK_INT >= 29) {
            values.put(MediaStore.Audio.Media.IS_PENDING, 1);
        }

        Uri uri = null;
        try {
            uri = resolver.insert(
                    MediaStore.Audio.Media.getContentUri(
                            MediaStore.VOLUME_EXTERNAL_PRIMARY),
                    values);

            if (uri == null) {
                throw new IOException("MediaStore insert failed");
            }

            try (BufferedInputStream in =
                         new BufferedInputStream(new FileInputStream(file));
                 OutputStream out =
                         new BufferedOutputStream(resolver.openOutputStream(uri))) {
                if (out == null) {
                    throw new IOException("MediaStore output stream unavailable");
                }

                byte[] buffer = new byte[1024 * 1024];
                int n;
                while ((n = in.read(buffer)) != -1) {
                    out.write(buffer, 0, n);
                }
                out.flush();
            }

            if (Build.VERSION.SDK_INT >= 29) {
                ContentValues done = new ContentValues();
                done.put(MediaStore.Audio.Media.IS_PENDING, 0);
                resolver.update(uri, done, null, null);
            }

            file.delete();
        } catch (Exception e) {
            if (uri != null) {
                try {
                    resolver.delete(uri, null, null);
                } catch (Exception ignored) {
                }
            }
            // Keep the private file: the next reconciliation will retry it.
        }
    }

    public static boolean reconcile(ContentResolver resolver, File directory) {
        File[] files = directory.listFiles((d, name) -> name.endsWith(".wav"));
        if (files == null || files.length == 0) {
            return true;
        }

        boolean allPublished = true;
        for (File file : files) {
            if (!file.exists()) {
                continue;
            }
            long before = file.length();
            publishReadyFile(resolver, file);
            if (file.exists() && file.length() == before) {
                allPublished = false;
            }
        }
        return allPublished;
    }

    private static Uri findPublishedItem(ContentResolver resolver, String displayName) {
        Uri collection = MediaStore.Audio.Media.getContentUri(
                MediaStore.VOLUME_EXTERNAL_PRIMARY);

        String[] projection = {
                MediaStore.Audio.Media._ID
        };

        String selection =
                MediaStore.Audio.Media.DISPLAY_NAME + "=? AND "
                        + MediaStore.Audio.Media.RELATIVE_PATH + "=? AND "
                        + MediaStore.Audio.Media.IS_PENDING + "=0";

        String[] args = {displayName, RELATIVE_PATH};

        try (Cursor cursor = resolver.query(
                collection, projection, selection, args, null)) {
            if (cursor != null && cursor.moveToFirst()) {
                long id = cursor.getLong(0);
                return ContentUris.withAppendedId(collection, id);
            }
        } catch (Exception ignored) {
        }
        return null;
    }
}
