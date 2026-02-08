package com.claudewatch.companion.wakeword

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.media.MediaRecorder
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.provider.Settings
import android.util.Log
import androidx.core.app.NotificationCompat
import com.claudewatch.companion.BuildConfig
import com.claudewatch.companion.MainActivity
import com.claudewatch.companion.R
import com.claudewatch.companion.SettingsActivity
import ai.picovoice.porcupine.PorcupineManager
import ai.picovoice.porcupine.PorcupineManagerCallback
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import java.io.File
import java.util.concurrent.TimeUnit

enum class WakeWordState { IDLE, RECORDING, SENDING, DONE }

class WakeWordService : Service() {

    companion object {
        private const val TAG = "WakeWordService"
        private const val CHANNEL_ID = "wake_word_channel"
        private const val NOTIFICATION_ID = 1001
        private const val SILENCE_THRESHOLD = 800
        private const val SILENCE_DURATION_MS = 1500L
        private const val GRACE_PERIOD_MS = 500L
        private const val MAX_RECORDING_MS = 30_000L
        private const val AMPLITUDE_POLL_MS = 200L

        private val _wakeWordState = MutableStateFlow(WakeWordState.IDLE)
        val wakeWordState = _wakeWordState.asStateFlow()

        private val _amplitude = MutableStateFlow(0f)
        val amplitude = _amplitude.asStateFlow()

        fun start(context: Context) {
            val intent = Intent(context, WakeWordService::class.java)
            context.startForegroundService(intent)
        }

        fun stop(context: Context) {
            val intent = Intent(context, WakeWordService::class.java)
            context.stopService(intent)
        }

        /** Stop recording and send immediately. Only acts when state is RECORDING. */
        fun requestStopRecording() {
            if (_wakeWordState.value != WakeWordState.RECORDING) return
            Log.i(TAG, "requestStopRecording: tap-to-stop triggered")
            instance?.scope?.launch(Dispatchers.Main) {
                instance?.stopRecordingAndSend()
            }
        }

        private var instance: WakeWordService? = null
    }

    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())
    private var porcupineManager: PorcupineManager? = null
    private var mediaRecorder: MediaRecorder? = null
    private var audioFile: File? = null
    private var silenceJob: Job? = null

    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        instance = this
        createNotificationChannel()
        startForeground(NOTIFICATION_ID, buildNotification("Listening for wake word..."))
        startPorcupine()
    }

    override fun onDestroy() {
        super.onDestroy()
        instance = null
        silenceJob?.cancel()
        stopRecorderSafely()
        stopPorcupineSafely()
        scope.cancel()
        httpClient.dispatcher.executorService.shutdown()
    }

    private fun createNotificationChannel() {
        val channel = NotificationChannel(
            CHANNEL_ID,
            "Wake Word Detection",
            NotificationManager.IMPORTANCE_LOW
        ).apply {
            description = "Listening for wake word"
            setShowBadge(false)
        }
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        val intent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP
        }
        val pendingIntent = PendingIntent.getActivity(
            this, 0, intent, PendingIntent.FLAG_IMMUTABLE
        )

        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("Claude Companion")
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentIntent(pendingIntent)
            .setOngoing(true)
            .setSilent(true)
            .build()
    }

    private fun updateNotification(text: String) {
        val manager = getSystemService(NotificationManager::class.java)
        manager.notify(NOTIFICATION_ID, buildNotification(text))
    }

    private fun startPorcupine() {
        try {
            val callback = PorcupineManagerCallback { _ ->
                Log.i(TAG, "Wake word detected!")
                onWakeWordDetected()
            }

            porcupineManager = PorcupineManager.Builder()
                .setAccessKey(BuildConfig.PORCUPINE_ACCESS_KEY)
                .setKeywordPath("hey-toadie_en_android_v4_0_0.ppn")
                .setSensitivity(0.7f)
                .build(this, callback)

            porcupineManager?.start()
            Log.i(TAG, "Porcupine started, listening for wake word")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to start Porcupine", e)
            stopSelf()
        }
    }

    private fun stopPorcupineSafely() {
        try {
            porcupineManager?.stop()
            porcupineManager?.delete()
        } catch (e: Exception) {
            Log.e(TAG, "Error stopping Porcupine", e)
        }
        porcupineManager = null
    }

    private fun onWakeWordDetected() {
        Log.i(TAG, "Setting state to RECORDING")
        _wakeWordState.value = WakeWordState.RECORDING

        // Acquire a brief wake lock to ensure the screen turns on
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        @Suppress("DEPRECATION")
        val wakeLock = pm.newWakeLock(
            PowerManager.FULL_WAKE_LOCK or PowerManager.ACQUIRE_CAUSES_WAKEUP,
            "claudewatch:wakeword"
        )
        wakeLock.acquire(3_000L) // released automatically after 3s

        // Launch activities using full-screen intent notification for reliable background launch
        launchWakeWordActivities()

        // Stop Porcupine first to release the mic
        stopPorcupineSafely()
        updateNotification("Recording...")
        startRecording()
    }

    private fun launchWakeWordActivities() {
        // Check if we can draw overlays (needed for background activity launch on Android 10+)
        val canDrawOverlays = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            Settings.canDrawOverlays(this)
        } else {
            true
        }

        // Launch MainActivity first (will be behind overlay)
        val mainIntent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_REORDER_TO_FRONT
        }

        // Launch fullscreen overlay activity on top
        val overlayIntent = Intent(this, WakeWordActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK
        }

        if (canDrawOverlays) {
            // Direct launch if we have overlay permission
            startActivity(mainIntent)
            startActivity(overlayIntent)
        } else {
            // Use full-screen intent notification as fallback
            val fullScreenPendingIntent = PendingIntent.getActivity(
                this, 0, overlayIntent,
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )

            val notification = NotificationCompat.Builder(this, CHANNEL_ID)
                .setContentTitle("Wake Word Detected")
                .setContentText("Tap to open")
                .setSmallIcon(R.drawable.ic_notification)
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setCategory(NotificationCompat.CATEGORY_CALL)
                .setFullScreenIntent(fullScreenPendingIntent, true)
                .setAutoCancel(true)
                .build()

            val manager = getSystemService(NotificationManager::class.java)
            manager.notify(NOTIFICATION_ID + 1, notification)

            // Also try direct launch
            startActivity(mainIntent)
            startActivity(overlayIntent)
        }
    }

    private fun startRecording() {
        try {
            audioFile = File.createTempFile("wakeword_", ".m4a", cacheDir)

            mediaRecorder = MediaRecorder().apply {
                setAudioSource(MediaRecorder.AudioSource.MIC)
                setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
                setAudioEncodingBitRate(128000)
                setAudioSamplingRate(44100)
                setOutputFile(audioFile?.absolutePath)
                prepare()
                start()
            }

            startSilenceDetection()
        } catch (e: Exception) {
            Log.e(TAG, "Failed to start recording", e)
            restartPorcupine()
        }
    }

    private fun startSilenceDetection() {
        silenceJob = scope.launch(Dispatchers.IO) {
            val startTime = System.currentTimeMillis()
            var silenceStart = 0L

            // Grace period - ignore silence at the start
            delay(GRACE_PERIOD_MS)

            while (true) {
                val elapsed = System.currentTimeMillis() - startTime
                if (elapsed >= MAX_RECORDING_MS) {
                    Log.i(TAG, "Max recording duration reached")
                    break
                }

                val amplitude = try {
                    mediaRecorder?.maxAmplitude ?: 0
                } catch (e: Exception) {
                    0
                }

                // Emit normalized amplitude (0-1) for UI feedback
                // maxAmplitude can go up to ~32767, but speech is typically 1000-10000
                val normalizedAmplitude = (amplitude / 10000f).coerceIn(0f, 1f)
                _amplitude.value = normalizedAmplitude

                if (amplitude < SILENCE_THRESHOLD) {
                    if (silenceStart == 0L) {
                        silenceStart = System.currentTimeMillis()
                    } else if (System.currentTimeMillis() - silenceStart >= SILENCE_DURATION_MS) {
                        Log.i(TAG, "Silence detected, stopping recording")
                        break
                    }
                } else {
                    silenceStart = 0L
                }

                delay(AMPLITUDE_POLL_MS)
            }

            scope.launch(Dispatchers.Main) {
                stopRecordingAndSend()
            }
        }
    }

    private fun stopRecorderSafely() {
        try {
            mediaRecorder?.apply {
                stop()
                release()
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error stopping recorder", e)
        }
        mediaRecorder = null
    }

    private fun stopRecordingAndSend() {
        silenceJob?.cancel()
        silenceJob = null
        stopRecorderSafely()

        Log.i(TAG, "Setting state to SENDING")
        _wakeWordState.value = WakeWordState.SENDING
        updateNotification("Sending to server...")

        val file = audioFile
        audioFile = null

        if (file != null && file.exists() && file.length() > 0) {
            scope.launch(Dispatchers.IO) {
                sendAudioToServer(file)
                file.delete()
                scope.launch(Dispatchers.Main) {
                    Log.i(TAG, "Setting state to DONE")
                    _wakeWordState.value = WakeWordState.DONE
                    restartPorcupine()
                }
            }
        } else {
            _wakeWordState.value = WakeWordState.DONE
            restartPorcupine()
        }
    }

    private fun sendAudioToServer(file: File) {
        try {
            val serverAddress = SettingsActivity.getServerAddress(this)
            val baseUrl = "http://${serverAddress.replace(":5567", ":5566")}"
            val url = "$baseUrl/transcribe"

            val request = Request.Builder()
                .url(url)
                .header("X-Response-Mode", "text")
                .post(file.asRequestBody("audio/mp4".toMediaType()))
                .build()

            val response = httpClient.newCall(request).execute()
            if (response.isSuccessful) {
                Log.i(TAG, "Audio sent successfully")
            } else {
                Log.e(TAG, "Failed to send audio: ${response.code}")
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error sending audio to server", e)
        }
    }

    private fun restartPorcupine() {
        updateNotification("Listening for wake word...")
        startPorcupine()
        // Delay setting IDLE to give activity time to observe DONE and finish
        scope.launch(Dispatchers.Main) {
            delay(500)
            _wakeWordState.value = WakeWordState.IDLE
        }
    }
}
