package com.example.memorycapture;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.IBinder;
import android.os.SystemClock;

import java.io.File;
import java.io.IOException;
import java.text.SimpleDateFormat;
import java.util.ArrayDeque;
import java.util.Date;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Always-on low-power capture:
 * - the microphone stream stays open, but capture is off during the rest
 *   window (NIGHT_START_HOUR..NIGHT_END_HOUR): the buffer is drained, nothing
 *   is written;
 * - a small in-memory pre-roll is continuously maintained;
 * - no Opus/Ogg encoder is used;
 * - speech-like activity starts PCM/WAV capture;
 * - capture stops after 30 s of quiet;
 * - active audio is checkpointed into 60 s WAV files so a crash cannot lose
 *   an hour-long conversation.
 */
public class RecordingService extends Service {
    public static final String ACTION_START = "com.example.memorycapture.START";
    public static final String ACTION_STOP = "com.example.memorycapture.STOP";

    private static final String CHANNEL_ID = "memory_capture";
    private static final int NOTIFICATION_ID = 42;
    private static final int SAMPLE_RATE = 16_000;
    private static final int CHANNELS = 1;

    private static final long PRE_ROLL_MS = 10_000L;
    private static final long QUIET_STOP_MS = 30_000L;
    private static final long CHECKPOINT_MS = 60_000L;
    private static final int FRAME_MS = 20;

    /** Plage de repos : capture désactivée entre NIGHT_START_HOUR et
     *  NIGHT_END_HOUR (bornes à ajuster selon l'usage). */
    private static final int NIGHT_START_HOUR = 22;
    private static final int NIGHT_END_HOUR = 7;

    private volatile boolean running = false;
    private volatile boolean recordingSpeech = false;
    private volatile boolean nightNotified = false;
    private Thread recordingThread;
    private AudioRecord recorder;
    private PcmWavSegmentWriter currentWriter;
    private long currentSegmentStartedElapsed;
    private long lastSpeechElapsed;
    private final ExecutorService publisher = Executors.newSingleThreadExecutor();
    private int sequence = 0;

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
        recoverPartials();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopRecording();
            return START_NOT_STICKY;
        }
        if (ACTION_START.equals(intent != null ? intent.getAction() : null)) {
            startRecordingIfNeeded();
        }
        return START_NOT_STICKY;
    }

    private void startRecordingIfNeeded() {
        if (running) return;
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            stopSelf();
            return;
        }

        startForeground(
                NOTIFICATION_ID,
                buildNotification("Veille vocale — en attente de parole"),
                android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE);

        running = true;
        recordingThread = new Thread(this::recordLoop, "memory-audio-vad");
        recordingThread.start();
    }

    private static boolean isNight() {
        int hour = java.util.Calendar.getInstance().get(java.util.Calendar.HOUR_OF_DAY);
        return hour >= NIGHT_START_HOUR || hour < NIGHT_END_HOUR;
    }

    private void recordLoop() {
        int minBuffer = AudioRecord.getMinBufferSize(
                SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT);
        if (minBuffer <= 0) minBuffer = SAMPLE_RATE / 2;

        int frameSamples = SAMPLE_RATE * FRAME_MS / 1000;
        int bufferSize = Math.max(minBuffer, frameSamples * 4 * 2);

        try {
            recorder = new AudioRecord(
                    MediaRecorder.AudioSource.MIC,
                    SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT,
                    bufferSize);

            if (recorder.getState() != AudioRecord.STATE_INITIALIZED) {
                throw new IOException("AudioRecord initialization failed");
            }

            recorder.startRecording();

            short[] frame = new short[frameSamples];
            ArrayDeque<short[]> preRoll = new ArrayDeque<>();
            int preRollFrames = Math.max(1, (int) (PRE_ROLL_MS / FRAME_MS));
            SpeechLikeDetector detector = new SpeechLikeDetector(2.2f);

            long quietSince = 0L;

            while (running) {
                int read = readFrame(frame);
                if (read != frameSamples) continue;

                short[] copy = frame.clone();
                preRoll.addLast(copy);
                while (preRoll.size() > preRollFrames) preRoll.removeFirst();

                boolean speech = detector.isSpeechLike(frame, 0, frame.length);
                boolean quiet = detector.isQuiet(frame, 0, frame.length);
                long now = SystemClock.elapsedRealtime();
                boolean night = isNight();

                if (night && !recordingSpeech) {
                    // Repos nocturne : aucune nouvelle capture ne démarre.
                    // On continue à lire pour vider le buffer micro, sans rien écrire.
                    preRoll.clear();
                    quietSince = 0L;
                    if (!nightNotified) {
                        nightNotified = true;
                        updateNotification("Repos nocturne — capture suspendue jusqu'à "
                                + NIGHT_END_HOUR + "h");
                    }
                    continue;
                }
                if (!night && nightNotified) {
                    nightNotified = false;
                    updateNotification("Veille vocale — en attente de parole");
                }

                if (!recordingSpeech && speech) {
                    beginSpeechCapture(preRoll);
                    recordingSpeech = true;
                    lastSpeechElapsed = now;
                    quietSince = 0L;
                    updateNotification("Enregistrement vocal actif");
                    continue;
                }

                if (recordingSpeech) {
                    currentWriter.write(frame, 0, frame.length);

                    if (speech) {
                        lastSpeechElapsed = now;
                        quietSince = 0L;
                    } else if (quiet) {
                        if (quietSince == 0L) quietSince = now;
                    } else {
                        quietSince = 0L;
                    }

                    if (now - currentSegmentStartedElapsed >= CHECKPOINT_MS) {
                        checkpointAndStartNext();
                        currentSegmentStartedElapsed = SystemClock.elapsedRealtime();
                    }

                    if (quietSince != 0L && now - quietSince >= QUIET_STOP_MS) {
                        finishSpeechCapture();
                        recordingSpeech = false;
                        quietSince = 0L;
                        updateNotification("Veille vocale — en attente de parole");
                    }
                }
            }
        } catch (Exception ignored) {
            // Finalized WAV checkpoints remain available after an error.
        } finally {
            try {
                if (currentWriter != null) {
                    finalizeAndQueue(currentWriter);
                }
            } catch (Exception ignored) {
            }
            currentWriter = null;
            recordingSpeech = false;

            try {
                if (recorder != null) recorder.stop();
            } catch (Exception ignored) {
            }
            if (recorder != null) recorder.release();
            recorder = null;

            stopForeground(STOP_FOREGROUND_REMOVE);
            stopSelf();
        }
    }

    private int readFrame(short[] frame) {
        int total = 0;
        while (running && total < frame.length) {
            int n = recorder.read(frame, total, frame.length - total, AudioRecord.READ_BLOCKING);
            if (n < 0) return n;
            total += n;
        }
        return total;
    }

    private void beginSpeechCapture(ArrayDeque<short[]> preRoll) throws IOException {
        currentWriter = newWriter();
        currentSegmentStartedElapsed = SystemClock.elapsedRealtime();
        for (short[] buffered : preRoll) {
            currentWriter.write(buffered, 0, buffered.length);
        }
    }

    private void finishSpeechCapture() throws IOException {
        if (currentWriter != null) {
            finalizeAndQueue(currentWriter);
            currentWriter = null;
        }
    }

    private void checkpointAndStartNext() throws IOException {
        PcmWavSegmentWriter old = currentWriter;
        currentWriter = newWriter();
        if (old != null) finalizeAndQueue(old);
    }

    private PcmWavSegmentWriter newWriter() throws IOException {
        File dir = new File(getFilesDir(), "recordings");
        if (!dir.exists() && !dir.mkdirs()) {
            throw new IOException("Cannot create recording directory");
        }
        String stamp = new SimpleDateFormat(
                "yyyy-MM-dd_HH-mm-ss-SSS", Locale.US).format(new Date());
        String filename = stamp + String.format(Locale.US, "_%04d.wav.part", sequence++);
        return new PcmWavSegmentWriter(new File(dir, filename), SAMPLE_RATE, CHANNELS);
    }

    private void finalizeAndQueue(PcmWavSegmentWriter writer) throws IOException {
        writer.close();
        File part = writer.getFile();
        String name = part.getName();
        if (!name.endsWith(".wav.part")) throw new IOException("Unexpected partial filename");

        File ready = new File(part.getParentFile(), name.substring(0, name.length() - 5));
        if (!part.renameTo(ready)) throw new IOException("Cannot finalize file");

        publisher.submit(() -> MediaStorePublisher.publishReadyFile(getContentResolver(), ready));
    }

    private void recoverPartials() {
        File dir = new File(getFilesDir(), "recordings");
        File[] parts = dir.listFiles((d, n) -> n.endsWith(".wav.part"));
        if (parts != null) {
            for (File part : parts) {
                // A partial WAV has not been checkpointed safely; discard it.
                // At most one active checkpoint interval is lost.
                part.delete();
            }
        }
        File[] ready = dir.listFiles((d, n) -> n.endsWith(".wav"));
        if (ready != null) {
            for (File file : ready) {
                publisher.submit(() -> MediaStorePublisher.publishReadyFile(getContentResolver(), file));
            }
        }
    }

    private void stopRecording() {
        running = false;
        try {
            if (recorder != null) recorder.stop();
        } catch (Exception ignored) {
        }
    }

    private Notification buildNotification(String text) {
        Intent stopIntent = new Intent(this, RecordingService.class);
        stopIntent.setAction(ACTION_STOP);
        PendingIntent stop = PendingIntent.getService(
                this, 2, stopIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        return new Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("MEMORY")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                .setOngoing(true)
                .addAction(new Notification.Action.Builder(null, "STOP", stop).build())
                .build();
    }

    private void updateNotification(String text) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) nm.notify(NOTIFICATION_ID, buildNotification(text));
    }

    private void createNotificationChannel() {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm == null) return;
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID,
                "Memory capture",
                NotificationManager.IMPORTANCE_LOW);
        nm.createNotificationChannel(channel);
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onDestroy() {
        running = false;
        publisher.shutdown();
        super.onDestroy();
    }
}
