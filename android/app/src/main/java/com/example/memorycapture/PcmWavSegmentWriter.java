package com.example.memorycapture;

import java.io.BufferedOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.RandomAccessFile;

/** Writes little-endian PCM16 mono audio to a WAV file. */
public final class PcmWavSegmentWriter {
    private final File file;
    private final int sampleRate;
    private final int channels;
    private final BufferedOutputStream out;
    private long dataBytes;
    private boolean closed;

    public PcmWavSegmentWriter(File file, int sampleRate, int channels) throws IOException {
        this.file = file;
        this.sampleRate = sampleRate;
        this.channels = channels;
        this.out = new BufferedOutputStream(new FileOutputStream(file), 64 * 1024);
        writeHeaderPlaceholder();
    }

    public File getFile() {
        return file;
    }

    public synchronized void write(short[] samples, int offset, int length) throws IOException {
        if (closed) throw new IOException("Writer already closed");
        for (int i = offset; i < offset + length; i++) {
            int v = samples[i];
            out.write(v & 0xff);
            out.write((v >>> 8) & 0xff);
        }
        dataBytes += (long) length * 2L;
    }

    public synchronized void close() throws IOException {
        if (closed) return;
        closed = true;
        out.flush();
        out.close();
        patchHeader();
    }

    private void writeHeaderPlaceholder() throws IOException {
        byte[] h = new byte[44];
        putAscii(h, 0, "RIFF");
        putIntLE(h, 4, 36);
        putAscii(h, 8, "WAVE");
        putAscii(h, 12, "fmt ");
        putIntLE(h, 16, 16);
        putShortLE(h, 20, (short) 1);
        putShortLE(h, 22, (short) channels);
        putIntLE(h, 24, sampleRate);
        int byteRate = sampleRate * channels * 2;
        putIntLE(h, 28, byteRate);
        putShortLE(h, 32, (short) (channels * 2));
        putShortLE(h, 34, (short) 16);
        putAscii(h, 36, "data");
        putIntLE(h, 40, 0);
        out.write(h);
    }

    private void patchHeader() throws IOException {
        try (RandomAccessFile raf = new RandomAccessFile(file, "rw")) {
            raf.seek(4);
            writeIntLE(raf, (int) Math.min(Integer.MAX_VALUE, 36L + dataBytes));
            raf.seek(40);
            writeIntLE(raf, (int) Math.min(Integer.MAX_VALUE, dataBytes));
        }
    }

    private static void putAscii(byte[] a, int p, String s) {
        for (int i = 0; i < s.length(); i++) a[p + i] = (byte) s.charAt(i);
    }

    private static void putIntLE(byte[] a, int p, int v) {
        a[p] = (byte) v;
        a[p + 1] = (byte) (v >>> 8);
        a[p + 2] = (byte) (v >>> 16);
        a[p + 3] = (byte) (v >>> 24);
    }

    private static void putShortLE(byte[] a, int p, short v) {
        a[p] = (byte) v;
        a[p + 1] = (byte) (v >>> 8);
    }

    private static void writeIntLE(RandomAccessFile raf, int v) throws IOException {
        raf.write(v & 0xff);
        raf.write((v >>> 8) & 0xff);
        raf.write((v >>> 16) & 0xff);
        raf.write((v >>> 24) & 0xff);
    }
}
