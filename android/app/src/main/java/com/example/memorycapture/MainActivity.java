package com.example.memorycapture;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.os.Build;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.view.Gravity;
import android.graphics.Color;

public class MainActivity extends Activity {
    private static final int REQ_AUDIO = 100;
    private static final int REQ_NOTIFICATIONS = 101;
    private TextView status;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        buildUi();
    }

    private void buildUi() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(48, 80, 48, 48);
        root.setGravity(Gravity.CENTER_HORIZONTAL);

        TextView title = new TextView(this);
        title.setText("MEMORY");
        title.setTextSize(28);
        title.setTextColor(Color.BLACK);
        title.setGravity(Gravity.CENTER);
        root.addView(title, new LinearLayout.LayoutParams(-1, -2));

        status = new TextView(this);
        status.setText("Prêt — veille vocale locale. Coupure automatique " +
                RecordingService.NIGHT_START_HOUR + "h → " +
                RecordingService.NIGHT_END_HOUR + "h.");
        status.setTextSize(16);
        status.setPadding(0, 40, 0, 40);
        root.addView(status, new LinearLayout.LayoutParams(-1, -2));

        Button start = new Button(this);
        start.setText("START MEMORY");
        start.setOnClickListener(v -> startMemory());
        root.addView(start, new LinearLayout.LayoutParams(-1, -2));

        Button stop = new Button(this);
        stop.setText("STOP MEMORY");
        stop.setOnClickListener(v -> stopMemory());
        root.addView(stop, new LinearLayout.LayoutParams(-1, -2));

        TextView help = new TextView(this);
        help.setText("WAV PCM 16 kHz / 16 bits. 10 s de pré-buffer, arrêt après 30 s de silence, sauvegarde par tranches d'1 min. Repos nocturne : capture suspendue pendant la plage de repos configurée. Aucun cloud, aucune IA.");
        help.setPadding(0, 40, 0, 0);
        root.addView(help, new LinearLayout.LayoutParams(-1, -2));

        setContentView(root);
    }

    private void startMemory() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, REQ_AUDIO);
            return;
        }
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQ_NOTIFICATIONS);
            return;
        }
        startServiceNow();
    }

    private void startServiceNow() {
        Intent intent = new Intent(this, RecordingService.class);
        intent.setAction(RecordingService.ACTION_START);
        startForegroundService(intent);
        status.setText("Memory actif : le micro reste en veille légère et démarre l'enregistrement sur une voix.");
    }

    private void stopMemory() {
        Intent intent = new Intent(this, RecordingService.class);
        intent.setAction(RecordingService.ACTION_STOP);
        startService(intent);
        status.setText("Arrêt demandé.");
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode == REQ_AUDIO) {
            if (grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                startMemory();
            } else {
                status.setText("Permission microphone refusée.");
            }
        } else if (requestCode == REQ_NOTIFICATIONS) {
            startServiceNow();
        }
    }
}
