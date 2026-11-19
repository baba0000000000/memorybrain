package com.example.memorycapture;

/**
 * Very small local voice-activity heuristic. It is deliberately not a speech
 * recognizer: it only decides whether a short frame looks sufficiently like
 * human speech to wake the recorder.
 */
public final class SpeechLikeDetector {
    private final float thresholdMultiplier;
    private float noiseRms = 350f;
    private int consecutiveSpeechFrames;
    private int consecutiveQuietFrames;

    public SpeechLikeDetector(float thresholdMultiplier) {
        this.thresholdMultiplier = thresholdMultiplier;
    }

    public boolean isSpeechLike(short[] samples, int offset, int length) {
        if (length < 160) return false;

        double sumSq = 0;
        int zeroCrossings = 0;
        int prev = samples[offset];
        for (int i = offset; i < offset + length; i++) {
            int v = samples[i];
            sumSq += (double) v * v;
            if ((v >= 0) != (prev >= 0)) zeroCrossings++;
            prev = v;
        }

        float rms = (float) Math.sqrt(sumSq / length);
        float zcr = (float) zeroCrossings / length;

        // Human speech tends to have substantial mid-band energy and a
        // moderate zero-crossing rate. This is only a trigger, not a classifier.
        float bandRatio = estimateSpeechBandRatio(samples, offset, length);
        float dynamicThreshold = Math.max(700f, noiseRms * thresholdMultiplier);
        boolean speechLike = rms > dynamicThreshold
                && zcr > 0.015f && zcr < 0.28f
                && bandRatio > 0.008f;

        if (speechLike) {
            consecutiveSpeechFrames++;
            consecutiveQuietFrames = 0;
        } else {
            consecutiveQuietFrames++;
            consecutiveSpeechFrames = 0;
            if (rms < noiseRms * 1.6f) {
                noiseRms = noiseRms * 0.98f + rms * 0.02f;
            }
        }

        return consecutiveSpeechFrames >= 2;
    }

    public boolean isQuiet(short[] samples, int offset, int length) {
        if (length < 160) return true;
        double sumSq = 0;
        for (int i = offset; i < offset + length; i++) {
            double v = samples[i];
            sumSq += v * v;
        }
        float rms = (float) Math.sqrt(sumSq / length);
        float dynamicThreshold = Math.max(500f, noiseRms * 1.8f);
        boolean quiet = rms < dynamicThreshold;
        if (quiet) {
            noiseRms = noiseRms * 0.98f + rms * 0.02f;
        }
        return quiet;
    }

    private static float estimateSpeechBandRatio(short[] s, int off, int len) {
        // Cheap spectral proxy: compare energy after a simple high-pass-ish
        // difference with total energy. It costs far less than an FFT.
        double total = 0;
        double diff = 0;
        int prev = s[off];
        for (int i = off; i < off + len; i++) {
            double v = s[i];
            total += v * v;
            double d = v - prev;
            diff += d * d;
            prev = s[i];
        }
        if (total <= 1) return 0;
        double ratio = diff / total;
        return (float) Math.min(1.0, ratio * 0.22);
    }
}
