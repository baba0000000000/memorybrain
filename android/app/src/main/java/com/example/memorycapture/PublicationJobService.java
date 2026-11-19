package com.example.memorycapture;

import android.app.job.JobInfo;
import android.app.job.JobParameters;
import android.app.job.JobScheduler;
import android.app.job.JobService;
import android.content.ComponentName;
import android.content.Context;

import java.io.File;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class PublicationJobService extends JobService {
    public static final int JOB_ID = 7001;

    private ExecutorService executor;

    public static void schedule(Context context) {
        JobScheduler scheduler =
                (JobScheduler) context.getSystemService(Context.JOB_SCHEDULER_SERVICE);
        if (scheduler == null) {
            return;
        }

        JobInfo job = new JobInfo.Builder(
                JOB_ID,
                new ComponentName(context, PublicationJobService.class))
                .setPersisted(true)
                .setMinimumLatency(60_000L)
                .setBackoffCriteria(
                        30_000L,
                        JobInfo.BACKOFF_POLICY_EXPONENTIAL)
                .build();

        scheduler.schedule(job);
    }

    @Override
    public boolean onStartJob(JobParameters params) {
        executor = Executors.newSingleThreadExecutor();
        executor.execute(() -> {
            File dir = new File(getFilesDir(), "recordings");
            boolean success = MediaStorePublisher.reconcile(
                    getContentResolver(), dir);
            jobFinished(params, !success);
            if (executor != null) {
                executor.shutdown();
                executor = null;
            }
        });
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        if (executor != null) {
            executor.shutdownNow();
            executor = null;
        }
        return true;
    }
}
