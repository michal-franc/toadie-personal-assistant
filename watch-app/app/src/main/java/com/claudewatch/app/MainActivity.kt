package com.claudewatch.app

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Intent
import android.content.pm.PackageManager
import android.media.MediaPlayer
import android.media.MediaRecorder
import android.os.Build
import android.os.Bundle
import android.os.VibrationEffect
import android.os.Vibrator
import android.util.Log
import android.view.View
import android.widget.Button
import android.widget.ImageButton
import android.widget.ProgressBar
import android.widget.TextView
import android.app.Activity
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import androidx.core.app.ActivityCompat
import kotlinx.coroutines.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.IOException

class MainActivity : Activity() {

    companion object {
        private const val TAG = "ClaudeWatch"
        private const val PERMISSION_REQUEST_CODE = 1001
        private const val NOTIFICATION_PERMISSION_CODE = 1002
        private const val CHANNEL_ID = "claude_response"
        private const val NOTIFICATION_ID = 1
        private const val POLL_INTERVAL_MS = 5000L  // 5 seconds
        private const val MAX_POLL_ATTEMPTS = 24     // 2 minutes max
    }

    private lateinit var recordButton: Button
    private lateinit var settingsButton: ImageButton
    private lateinit var statusText: TextView
    private lateinit var progressBar: ProgressBar

    private var mediaRecorder: MediaRecorder? = null
    private var mediaPlayer: MediaPlayer? = null
    private var audioFile: File? = null
    private var isRecording = false

    private val coroutineScope = CoroutineScope(Dispatchers.Main + Job())
    private val httpClient = OkHttpClient.Builder()
        .connectTimeout(30, java.util.concurrent.TimeUnit.SECONDS)
        .writeTimeout(60, java.util.concurrent.TimeUnit.SECONDS)
        .readTimeout(30, java.util.concurrent.TimeUnit.SECONDS)
        .build()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        recordButton = findViewById(R.id.recordButton)
        settingsButton = findViewById(R.id.settingsButton)
        statusText = findViewById(R.id.statusText)
        progressBar = findViewById(R.id.progressBar)

        recordButton.setOnClickListener {
            onRecordButtonClick()
        }

        settingsButton.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }

        createNotificationChannel()
        requestNotificationPermission()

        // Auto-start recording on launch
        autoStartRecording()
    }

    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
                ActivityCompat.requestPermissions(
                    this,
                    arrayOf(Manifest.permission.POST_NOTIFICATIONS),
                    NOTIFICATION_PERMISSION_CODE
                )
            }
        }
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val name = "Claude Responses"
            val descriptionText = "Notifications for Claude responses"
            val importance = NotificationManager.IMPORTANCE_HIGH
            val channel = NotificationChannel(CHANNEL_ID, name, importance).apply {
                description = descriptionText
                enableVibration(true)
            }
            val notificationManager = getSystemService(NotificationManager::class.java)
            notificationManager.createNotificationChannel(channel)
        }
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        // Second button press - toggle recording
        if (isRecording) {
            stopRecordingAndSend()
        } else {
            autoStartRecording()
        }
    }

    private fun autoStartRecording() {
        if (checkPermission()) {
            if (!isRecording) {
                startRecording()
            }
        } else {
            ActivityCompat.requestPermissions(
                this,
                arrayOf(Manifest.permission.RECORD_AUDIO),
                PERMISSION_REQUEST_CODE
            )
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        coroutineScope.cancel()
        stopRecording()
        mediaPlayer?.release()
        mediaPlayer = null
        httpClient.dispatcher.executorService.shutdown()
    }

    private fun onRecordButtonClick() {
        if (checkPermission()) {
            toggleRecording()
        } else {
            ActivityCompat.requestPermissions(
                this,
                arrayOf(Manifest.permission.RECORD_AUDIO),
                PERMISSION_REQUEST_CODE
            )
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == PERMISSION_REQUEST_CODE) {
            if (grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                startRecording()
            } else {
                showStatus("Microphone permission denied", isError = true)
            }
        }
    }

    private fun checkPermission(): Boolean {
        return ContextCompat.checkSelfPermission(
            this,
            Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
    }

    private fun toggleRecording() {
        if (isRecording) {
            stopRecordingAndSend()
        } else {
            startRecording()
        }
    }

    private fun startRecording() {
        try {
            // Create temp file for recording
            audioFile = File.createTempFile("recording_", ".m4a", cacheDir)

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

            isRecording = true
            vibrate(50)
            updateUI()
            showStatus("Recording...")
            Log.d(TAG, "Recording started: ${audioFile?.absolutePath}")

        } catch (e: Exception) {
            Log.e(TAG, "Failed to start recording", e)
            showStatus("Failed to start recording", isError = true)
            cleanupRecording()
        }
    }

    private fun stopRecordingAndSend() {
        stopRecording()
        vibrate(100)
        sendRecording()
    }

    private fun stopRecording() {
        try {
            mediaRecorder?.apply {
                stop()
                release()
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error stopping recording", e)
        }
        mediaRecorder = null
        isRecording = false
        updateUI()
    }

    private fun sendRecording() {
        val file = audioFile ?: run {
            showStatus("No recording to send", isError = true)
            return
        }

        showStatus("Sending...")
        progressBar.visibility = View.VISIBLE
        recordButton.isEnabled = false

        coroutineScope.launch {
            try {
                val result = withContext(Dispatchers.IO) {
                    sendToServer(file)
                }

                if (result.isSuccess) {
                    val responseBody = result.getOrNull() ?: ""
                    try {
                        val json = JSONObject(responseBody)
                        val requestId = json.optString("request_id", "")
                        val transcript = json.optString("transcript", "")
                        val responseEnabled = json.optBoolean("response_enabled", false)

                        if (requestId.isNotEmpty() && responseEnabled) {
                            showStatus("Waiting for Claude...")
                            // Start polling for response
                            pollForResponse(requestId)
                        } else {
                            // Response disabled or no request ID
                            showStatus("Sent to Claude")
                            progressBar.visibility = View.GONE
                            recordButton.isEnabled = true
                        }
                    } catch (e: Exception) {
                        showStatus("Sent successfully!")
                        progressBar.visibility = View.GONE
                        recordButton.isEnabled = true
                    }

                    vibrate(50)
                    file.delete()
                    audioFile = null
                } else {
                    showStatus("Failed: ${result.exceptionOrNull()?.message}", isError = true)
                    vibrate(longArrayOf(0, 100, 100, 100))
                    progressBar.visibility = View.GONE
                    recordButton.isEnabled = true
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error sending recording", e)
                showStatus("Error: ${e.message}", isError = true)
                vibrate(longArrayOf(0, 100, 100, 100))
                progressBar.visibility = View.GONE
                recordButton.isEnabled = true
            }
        }
    }

    private fun pollForResponse(requestId: String) {
        coroutineScope.launch {
            var attempts = 0
            while (attempts < MAX_POLL_ATTEMPTS) {
                // First poll faster (1.5s), then normal interval (5s)
                val delayMs = if (attempts == 0) 1500L else POLL_INTERVAL_MS
                delay(delayMs)
                attempts++

                try {
                    val result = withContext(Dispatchers.IO) {
                        checkResponse(requestId)
                    }

                    if (result != null) {
                        val status = result.optString("status", "")
                        if (status == "completed") {
                            val response = result.optString("response", "No response")
                            val type = result.optString("type", "text")

                            progressBar.visibility = View.GONE
                            recordButton.isEnabled = true

                            if (type == "audio") {
                                val audioUrl = result.optString("audio_url", "")
                                if (audioUrl.isNotEmpty()) {
                                    showStatus("Playing response...")
                                    playAudioResponse(audioUrl, requestId)
                                } else {
                                    showNotification(response)
                                    showStatus("Response received!")
                                    sendAck(requestId)
                                }
                            } else {
                                showNotification(response)
                                showStatus("Response received!")
                                sendAck(requestId)
                            }
                            vibrate(longArrayOf(0, 100, 50, 100))
                            return@launch
                        } else if (status == "disabled") {
                            progressBar.visibility = View.GONE
                            recordButton.isEnabled = true
                            showStatus("Sent to Claude")
                            return@launch
                        } else if (status == "not_found") {
                            Log.w(TAG, "Request not found on server")
                            break
                        }
                        // status == "pending", continue polling
                        showStatus("Waiting... (${attempts})")
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "Error polling for response", e)
                }
            }

            // Timeout or error
            progressBar.visibility = View.GONE
            recordButton.isEnabled = true
            showStatus("Response timeout")
        }
    }

    private fun checkResponse(requestId: String): JSONObject? {
        return try {
            val baseUrl = SettingsActivity.getServerUrl(this).replace("/transcribe", "")
            val url = "$baseUrl/api/response/$requestId"

            val request = Request.Builder()
                .url(url)
                .get()
                .build()

            val response = httpClient.newCall(request).execute()

            if (response.isSuccessful) {
                val body = response.body?.string() ?: "{}"
                JSONObject(body)
            } else {
                null
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error checking response", e)
            null
        }
    }

    private fun playAudioResponse(audioPath: String, requestId: String) {
        coroutineScope.launch {
            try {
                val audioData = withContext(Dispatchers.IO) {
                    downloadAudio(audioPath)
                }

                if (audioData != null) {
                    // Save to temp file and play
                    val tempFile = File.createTempFile("response_", ".mp3", cacheDir)
                    tempFile.writeBytes(audioData)

                    mediaPlayer?.release()
                    mediaPlayer = MediaPlayer().apply {
                        setDataSource(tempFile.absolutePath)
                        setOnCompletionListener {
                            showStatus("Done")
                            tempFile.delete()
                            it.release()
                            // Send ack after audio finishes playing
                            sendAck(requestId)
                        }
                        setOnErrorListener { _, _, _ ->
                            showStatus("Audio error", isError = true)
                            tempFile.delete()
                            true
                        }
                        prepare()
                        start()
                    }
                    showStatus("Playing...")
                } else {
                    showStatus("Audio download failed", isError = true)
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error playing audio", e)
                showStatus("Audio error", isError = true)
            }
        }
    }

    private fun downloadAudio(audioPath: String): ByteArray? {
        return try {
            val baseUrl = SettingsActivity.getServerUrl(this).replace("/transcribe", "")
            val url = "$baseUrl$audioPath"

            val request = Request.Builder()
                .url(url)
                .get()
                .build()

            val response = httpClient.newCall(request).execute()

            if (response.isSuccessful) {
                response.body?.bytes()
            } else {
                null
            }
        } catch (e: Exception) {
            Log.e(TAG, "Error downloading audio", e)
            null
        }
    }

    private fun sendAck(requestId: String) {
        coroutineScope.launch {
            try {
                withContext(Dispatchers.IO) {
                    val baseUrl = SettingsActivity.getServerUrl(this@MainActivity).replace("/transcribe", "")
                    val url = "$baseUrl/api/response/$requestId/ack"

                    val request = Request.Builder()
                        .url(url)
                        .post("".toByteArray().toRequestBody(null))
                        .build()

                    val response = httpClient.newCall(request).execute()
                    Log.d(TAG, "Ack sent for $requestId: ${response.code}")
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error sending ack", e)
            }
        }
    }

    private fun showNotification(message: String) {
        // Truncate for notification, full text will be in expanded view
        val shortMessage = if (message.length > 100) message.take(100) + "..." else message

        val builder = NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle("Claude")
            .setContentText(shortMessage)
            .setStyle(NotificationCompat.BigTextStyle().bigText(message))
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setAutoCancel(true)
            .setVibrate(longArrayOf(0, 200, 100, 200))

        try {
            NotificationManagerCompat.from(this).notify(NOTIFICATION_ID, builder.build())
        } catch (e: SecurityException) {
            Log.e(TAG, "No notification permission", e)
            // Show on screen instead
            showStatus(shortMessage)
        }
    }

    private suspend fun sendToServer(file: File): Result<String> {
        return try {
            val serverUrl = SettingsActivity.getServerUrl(this@MainActivity)
            val requestBody = file.asRequestBody("audio/mp4".toMediaType())

            val request = Request.Builder()
                .url(serverUrl)
                .post(requestBody)
                .build()

            val response = httpClient.newCall(request).execute()

            if (response.isSuccessful) {
                val body = response.body?.string() ?: ""
                Log.d(TAG, "Server response: $body")
                Result.success(body)
            } else {
                val error = "Server error: ${response.code}"
                Log.e(TAG, error)
                Result.failure(IOException(error))
            }
        } catch (e: Exception) {
            Log.e(TAG, "Network error", e)
            Result.failure(e)
        }
    }

    private fun cleanupRecording() {
        mediaRecorder?.release()
        mediaRecorder = null
        audioFile?.delete()
        audioFile = null
        isRecording = false
        updateUI()
    }

    private fun updateUI() {
        recordButton.text = if (isRecording) "Stop & Send" else "Record"
        recordButton.setBackgroundColor(
            if (isRecording)
                ContextCompat.getColor(this, android.R.color.holo_red_dark)
            else
                ContextCompat.getColor(this, android.R.color.holo_blue_dark)
        )
    }

    private fun showStatus(message: String, isError: Boolean = false) {
        statusText.text = message
        statusText.setTextColor(
            if (isError)
                ContextCompat.getColor(this, android.R.color.holo_red_light)
            else
                ContextCompat.getColor(this, android.R.color.white)
        )
    }

    private fun vibrate(duration: Long) {
        val vibrator = getSystemService(Vibrator::class.java)
        vibrator?.vibrate(VibrationEffect.createOneShot(duration, VibrationEffect.DEFAULT_AMPLITUDE))
    }

    private fun vibrate(pattern: LongArray) {
        val vibrator = getSystemService(Vibrator::class.java)
        vibrator?.vibrate(VibrationEffect.createWaveform(pattern, -1))
    }
}
