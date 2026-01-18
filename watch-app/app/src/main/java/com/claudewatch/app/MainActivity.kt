package com.claudewatch.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.media.MediaRecorder
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
import androidx.core.content.ContextCompat
import androidx.core.app.ActivityCompat
import kotlinx.coroutines.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.asRequestBody
import java.io.File
import java.io.IOException

class MainActivity : Activity() {

    companion object {
        private const val TAG = "ClaudeWatch"
        private const val PERMISSION_REQUEST_CODE = 1001
    }

    private lateinit var recordButton: Button
    private lateinit var settingsButton: ImageButton
    private lateinit var statusText: TextView
    private lateinit var progressBar: ProgressBar

    private var mediaRecorder: MediaRecorder? = null
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

        // Auto-start recording on launch
        autoStartRecording()
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
                    showStatus("Sent successfully!")
                    vibrate(50)
                    // Cleanup the file after successful send
                    file.delete()
                    audioFile = null
                } else {
                    showStatus("Failed: ${result.exceptionOrNull()?.message}", isError = true)
                    vibrate(longArrayOf(0, 100, 100, 100))
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error sending recording", e)
                showStatus("Error: ${e.message}", isError = true)
                vibrate(longArrayOf(0, 100, 100, 100))
            } finally {
                progressBar.visibility = View.GONE
                recordButton.isEnabled = true
            }
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
