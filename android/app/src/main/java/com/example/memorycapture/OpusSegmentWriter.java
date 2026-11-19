package com.example.memorycapture;

import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaCodecList;
import android.media.MediaFormat;
import android.media.MediaMuxer;

import java.io.File;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;

/**
 * Encodes PCM 16-bit mono audio to Opus and muxes it into an Ogg container.
 * The output file is only considered final after close() succeeds.
 */
public final class OpusSegmentWriter implements AutoCloseable {
    private static final String MIME = MediaFormat.MIMETYPE_AUDIO_OPUS;
    private static final int BIT_RATE = 40_000;
    private static final long TIMEOUT_US = 10_000;

    private final File file;
    private final int sampleRate;
    private final MediaCodec codec;
    private final MediaMuxer muxer;

    private boolean muxerStarted;
    private boolean inputEosQueued;
    private boolean closed;
    private int trackIndex = -1;
    private long samplesSubmitted;

    public OpusSegmentWriter(File file, int sampleRate, int channels) throws IOException {
        this.file = file;
        this.sampleRate = sampleRate;

        MediaFormat format = MediaFormat.createAudioFormat(MIME, sampleRate, channels);
        format.setInteger(MediaFormat.KEY_BIT_RATE, BIT_RATE);

        String encoderName = new MediaCodecList(MediaCodecList.REGULAR_CODECS)
                .findEncoderForFormat(format);
        if (encoderName == null) {
            throw new IOException(
                    "No Android Opus encoder for " + sampleRate + " Hz / " + channels + " channel(s)");
        }

        try {
            MediaCodecInfo.CodecCapabilities caps = null;
            for (MediaCodecInfo info :
                    new MediaCodecList(MediaCodecList.REGULAR_CODECS).getCodecInfos()) {
                if (info.getName().equals(encoderName)) {
                    caps = info.getCapabilitiesForType(MIME);
                    break;
                }
            }

            if (caps != null && caps.getEncoderCapabilities() != null
                    && caps.getEncoderCapabilities().isBitrateModeSupported(
                    MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_VBR)) {
                format.setInteger(
                        MediaFormat.KEY_BITRATE_MODE,
                        MediaCodecInfo.EncoderCapabilities.BITRATE_MODE_VBR);
            }

            codec = MediaCodec.createByCodecName(encoderName);
            codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
            codec.start();

            muxer = new MediaMuxer(
                    file.getAbsolutePath(),
                    MediaMuxer.OutputFormat.MUXER_OUTPUT_OGG);
        } catch (Exception e) {
            throw new IOException("Unable to initialize Android Opus/Ogg encoder", e);
        }
    }

    public synchronized void write(short[] samples, int offset, int length) throws IOException {
        if (closed) {
            throw new IOException("Opus writer already closed");
        }

        int remaining = length;
        int position = offset;

        while (remaining > 0) {
            int inputIndex = codec.dequeueInputBuffer(TIMEOUT_US);
            if (inputIndex >= 0) {
                ByteBuffer input = codec.getInputBuffer(inputIndex);
                if (input == null) {
                    throw new IOException("Opus encoder input buffer unavailable");
                }

                input.clear();
                input.order(ByteOrder.LITTLE_ENDIAN);

                int count = Math.min(remaining, input.remaining() / 2);
                for (int i = 0; i < count; i++) {
                    input.putShort(samples[position + i]);
                }

                long ptsUs = samplesSubmitted * 1_000_000L / sampleRate;
                codec.queueInputBuffer(inputIndex, 0, count * 2, ptsUs, 0);

                samplesSubmitted += count;
                position += count;
                remaining -= count;
            }

            drain(false);
        }
    }

    private void drain(boolean endOfStream) throws IOException {
        if (endOfStream && !inputEosQueued) {
            int inputIndex;
            do {
                inputIndex = codec.dequeueInputBuffer(TIMEOUT_US);
                if (inputIndex >= 0) {
                    break;
                }
                drain(false);
            } while (true);

            long ptsUs = samplesSubmitted * 1_000_000L / sampleRate;
            codec.queueInputBuffer(
                    inputIndex, 0, 0, ptsUs, MediaCodec.BUFFER_FLAG_END_OF_STREAM);
            inputEosQueued = true;
        }

        MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
        int idleRounds = 0;

        while (true) {
            int outputIndex = codec.dequeueOutputBuffer(info, TIMEOUT_US);

            if (outputIndex == MediaCodec.INFO_TRY_AGAIN_LATER) {
                if (!endOfStream) {
                    return;
                }
                if (++idleRounds > 100) {
                    throw new IOException("Timed out waiting for Opus encoder EOS");
                }
                continue;
            }

            idleRounds = 0;

            if (outputIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                if (muxerStarted) {
                    throw new IOException("Opus output format changed twice");
                }
                trackIndex = muxer.addTrack(codec.getOutputFormat());
                muxer.start();
                muxerStarted = true;
                continue;
            }

            if (outputIndex == MediaCodec.INFO_OUTPUT_BUFFERS_CHANGED) {
                continue;
            }

            if (outputIndex < 0) {
                continue;
            }

            ByteBuffer output = codec.getOutputBuffer(outputIndex);
            if (output != null
                    && info.size > 0
                    && muxerStarted
                    && (info.flags & MediaCodec.BUFFER_FLAG_CODEC_CONFIG) == 0) {
                output.position(info.offset);
                output.limit(info.offset + info.size);
                muxer.writeSampleData(trackIndex, output, info);
            }

            boolean eos = (info.flags & MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0;
            codec.releaseOutputBuffer(outputIndex, false);

            if (eos) {
                return;
            }
        }
    }

    @Override
    public synchronized void close() throws IOException {
        if (closed) {
            return;
        }

        IOException failure = null;
        try {
            drain(true);
        } catch (IOException e) {
            failure = e;
        } finally {
            try {
                codec.stop();
            } catch (Exception ignored) {
            }
            try {
                codec.release();
            } catch (Exception ignored) {
            }

            try {
                if (muxerStarted) {
                    muxer.stop();
                }
            } catch (Exception e) {
                if (failure == null) {
                    failure = new IOException("Unable to finalize Ogg container", e);
                }
            } finally {
                try {
                    muxer.release();
                } catch (Exception ignored) {
                }
            }

            closed = true;
        }

        if (failure != null) {
            throw failure;
        }
    }

    public File getFile() {
        return file;
    }
}
