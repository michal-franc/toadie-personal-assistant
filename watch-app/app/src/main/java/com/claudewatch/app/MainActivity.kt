package com.claudewatch.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.drawable.GradientDrawable
import android.media.MediaPlayer
import android.media.MediaRecorder
import android.os.Bundle
import android.os.VibrationEffect
import android.os.Vibrator
import android.util.Log
import android.view.View
import android.view.animation.AccelerateDecelerateInterpolator
import android.view.animation.OvershootInterpolator
import android.widget.Button
import android.widget.FrameLayout
import android.widget.ImageButton
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import android.app.Activity
import androidx.core.content.ContextCompat
import androidx.core.app.ActivityCompat
import androidx.wear.widget.WearableRecyclerView
import android.os.Handler
import android.os.Looper
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.collectLatest
import org.json.JSONObject
import java.io.File
import com.claudewatch.app.chat.WatchChatAdapter
import com.claudewatch.app.network.*
import com.claudewatch.app.relay.RelayClient

class MainActivity : Activity() {

    enum class IntentAction {
        IGNORE, START_RECORDING, STOP_AND_SEND, PAUSE_AUDIO, ABORT, NONE
    }

    companion object {
        private const val TAG = "ClaudeWatch"
        private const val PERMISSION_REQUEST_CODE = 1001
        private const val FADE_MS = 200L
        private const val SCALE_MS = 250L

        fun resolveIntentAction(
            fromPermission: Boolean,
            autoRecord: Boolean,
            hasPermission: Boolean,
            isRecording: Boolean,
            isPlayingAudio: Boolean,
            claudeStatus: String
        ): IntentAction = when {
            fromPermission -> IntentAction.IGNORE
            autoRecord && isRecording -> IntentAction.STOP_AND_SEND
            autoRecord && hasPermission -> IntentAction.START_RECORDING
            autoRecord -> IntentAction.NONE
            isPlayingAudio -> IntentAction.PAUSE_AUDIO
            claudeStatus == "thinking" -> IntentAction.ABORT
            isRecording -> IntentAction.STOP_AND_SEND
            else -> IntentAction.NONE
        }
    }

    // UI elements
    private lateinit var recordButton: TextView
    private lateinit var abortButton: TextView
    private lateinit var audioControls: LinearLayout
    private lateinit var replayButton: TextView
    private lateinit var pauseButton: TextView
    private lateinit var doneButton: TextView
    private lateinit var settingsButton: ImageButton
    private lateinit var settingsButtonBottom: ImageView
    private lateinit var speakerToggle: ImageView
    private lateinit var thinkingOverlay: FrameLayout

    // Connection bar
    private lateinit var connectionDot: View
    private lateinit var connectionText: TextView

    // State indicator
    private lateinit var stateIndicator: TextView

    // Disconnected overlay
    private lateinit var disconnectedOverlay: FrameLayout

    // Chat
    private lateinit var chatRecyclerView: WearableRecyclerView
    private lateinit var chatAdapter: WatchChatAdapter

    // Prompt overlay
    private lateinit var promptOverlay: LinearLayout
    private lateinit var promptTitle: TextView
    private lateinit var promptContext: TextView
    private lateinit var promptQuestion: TextView
    private lateinit var promptOptionsContainer: LinearLayout

    // State (local watch-only)
    private var mediaRecorder: MediaRecorder? = null
    private var mediaPlayer: MediaPlayer? = null
    private var audioFile: File? = null
    private var isRecording = false
    private var isPlayingAudio = false
    private var voiceResponseEnabled = false
    private var currentAudioFile: File? = null
    private var currentRequestId: String? = null

    // WebSocket client (now routes through phone relay)
    private lateinit var wsClient: WatchWebSocketClient

    private val coroutineScope = CoroutineScope(Dispatchers.Main + Job())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        // Initialize relay client
        RelayClient.init(this)

        initViews()
        setupChat()
        setupClickListeners()
        setupWebSocket()
        collectFlows()

        // Cold-start auto-record (from RecordActivity trampoline / hardware button)
        if (intent?.getBooleanExtra("auto_record", false) == true) {
            Handler(Looper.getMainLooper()).postDelayed({
                if (checkPermission() && !isRecording) startRecording()
            }, 500)
        }
    }

    private fun initViews() {
        recordButton = findViewById(R.id.recordButton)
        abortButton = findViewById(R.id.abortButton)
        audioControls = findViewById(R.id.audioControls)
        replayButton = findViewById(R.id.replayButton)
        pauseButton = findViewById(R.id.pauseButton)
        doneButton = findViewById(R.id.doneButton)
        settingsButton = findViewById(R.id.settingsButton)
        settingsButtonBottom = findViewById(R.id.settingsButtonBottom)
        speakerToggle = findViewById(R.id.speakerToggle)
        thinkingOverlay = findViewById(R.id.thinkingOverlay)

        connectionDot = findViewById(R.id.connectionDot)
        connectionText = findViewById(R.id.connectionText)
        stateIndicator = findViewById(R.id.stateIndicator)

        chatRecyclerView = findViewById(R.id.chatRecyclerView)
        disconnectedOverlay = findViewById(R.id.disconnectedOverlay)

        promptOverlay = findViewById(R.id.promptOverlay)
        promptTitle = findViewById(R.id.promptTitle)
        promptContext = findViewById(R.id.promptContext)
        promptQuestion = findViewById(R.id.promptQuestion)
        promptOptionsContainer = findViewById(R.id.promptOptionsContainer)
    }

    private fun setupChat() {
        chatAdapter = WatchChatAdapter()
        chatRecyclerView.apply {
            isEdgeItemsCenteringEnabled = false
            clipChildren = false
            clipToPadding = false
            layoutManager = androidx.recyclerview.widget.LinearLayoutManager(this@MainActivity).apply {
                stackFromEnd = true
            }
            adapter = chatAdapter
            addItemDecoration(WatchChatAdapter.OverlapDecoration())
        }
    }

    private fun setupClickListeners() {
        recordButton.setOnClickListener { onRecordButtonClick() }
        abortButton.setOnClickListener { onAbortClick() }
        replayButton.setOnClickListener { onReplayClick() }
        pauseButton.setOnClickListener { onPauseClick() }
        doneButton.setOnClickListener { onDoneClick() }
        settingsButton.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }
        settingsButtonBottom.setOnClickListener {
            startActivity(Intent(this, SettingsActivity::class.java))
        }

        // Load persisted voice response preference
        val prefs = getSharedPreferences("ClaudeWatchPrefs", MODE_PRIVATE)
        voiceResponseEnabled = prefs.getBoolean("voice_response_enabled", false)
        updateSpeakerIcon()

        speakerToggle.setOnClickListener {
            voiceResponseEnabled = !voiceResponseEnabled
            prefs.edit().putBoolean("voice_response_enabled", voiceResponseEnabled).apply()
            updateSpeakerIcon()
            vibrate(50)
        }
    }

    private fun updateSpeakerIcon() {
        val color = if (voiceResponseEnabled) "#0099FF" else "#888888"
        speakerToggle.setColorFilter(android.graphics.Color.parseColor(color))
    }

    private fun setupWebSocket() {
        // No server address needed — relay goes through phone
        wsClient = WatchWebSocketClient()
        wsClient.connect()
    }

    private fun collectFlows() {
        // Connection status
        coroutineScope.launch {
            wsClient.connectionStatus.collectLatest { status ->
                updateConnectionIndicator(status)
            }
        }

        // Claude state
        coroutineScope.launch {
            wsClient.claudeState.collectLatest { state ->
                onClaudeStateChanged(state)
            }
        }

        // Chat messages
        coroutineScope.launch {
            wsClient.chatMessages.collectLatest { messages ->
                chatAdapter.submitMessages(messages) {
                    val count = chatAdapter.itemCount
                    if (count > 0) {
                        chatRecyclerView.smoothScrollToPosition(count - 1)
                    }
                }
            }
        }

        // Prompt / permission
        coroutineScope.launch {
            wsClient.currentPrompt.collectLatest { prompt ->
                if (prompt != null) {
                    showPrompt(prompt)
                } else {
                    hidePrompt()
                }
            }
        }
    }

    private fun updateConnectionIndicator(status: ConnectionStatus) {
        val color = when (status) {
            ConnectionStatus.CONNECTED -> android.R.color.holo_green_light
            ConnectionStatus.CONNECTING -> android.R.color.holo_orange_light
            ConnectionStatus.DISCONNECTED -> android.R.color.holo_red_light
        }
        connectionDot.setBackgroundColor(ContextCompat.getColor(this, color))

        // Show/hide disconnected overlay on chat
        val isConnected = status == ConnectionStatus.CONNECTED
        if (!isConnected) {
            fadeIn(disconnectedOverlay)
            chatRecyclerView.alpha = 0.4f
        } else {
            fadeOut(disconnectedOverlay)
            chatRecyclerView.alpha = 1f
        }

        // Update record button enabled state
        updateUIState()
    }

    private fun onClaudeStateChanged(state: ClaudeState) {
        // When state transitions to "speaking", fetch audio
        if (state.status == "speaking" && state.requestId != null && !isPlayingAudio) {
            fetchAudioForRequest(state.requestId)
        }
        updateUIState()
    }

    private fun fetchAudioForRequest(requestId: String) {
        coroutineScope.launch {
            try {
                val result = withContext(Dispatchers.IO) {
                    checkResponse(requestId)
                }
                if (result != null) {
                    val status = result.optString("status", "")
                    if (status == "completed") {
                        val type = result.optString("type", "text")
                        if (type == "audio") {
                            val audioUrl = result.optString("audio_url", "")
                            if (audioUrl.isNotEmpty()) {
                                downloadAndPlayAudio(audioUrl, requestId)
                            }
                        }
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error fetching audio for request $requestId", e)
            }
        }
    }

    // --- Prompt / Permission handling ---

    private fun showPrompt(prompt: ClaudePrompt) {
        promptTitle.text = prompt.title ?: if (prompt.isPermission) "Permission" else "Prompt"

        if (!prompt.context.isNullOrEmpty()) {
            promptContext.text = prompt.context
            promptContext.visibility = View.VISIBLE
        } else {
            promptContext.visibility = View.GONE
        }

        promptQuestion.text = prompt.question

        // Build option buttons dynamically
        promptOptionsContainer.removeAllViews()

        if (prompt.isPermission && prompt.options.isEmpty()) {
            // Legacy permission: simple Allow/Deny
            addPromptButton("Allow", "#4CAF50") {
                respondToPermission(prompt.requestId ?: return@addPromptButton, "allow")
            }
            addPromptButton("Deny", "#F44336") {
                respondToPermission(prompt.requestId ?: return@addPromptButton, "deny")
            }
        } else {
            // Dynamic options
            for (option in prompt.options) {
                val color = if (option.selected) "#2962FF" else "#555555"
                val label = if (option.description.isNotEmpty()) {
                    "${option.label}\n${option.description}"
                } else {
                    option.label
                }
                addPromptButton(label, color) {
                    if (prompt.isPermission) {
                        respondToPermission(prompt.requestId ?: return@addPromptButton, option.label.lowercase())
                    } else {
                        respondToPrompt(option.num)
                    }
                }
            }
        }

        promptOverlay.alpha = 0f
        promptOverlay.visibility = View.VISIBLE
        promptOverlay.animate()
            .alpha(1f)
            .setDuration(FADE_MS)
            .start()

        vibrate(longArrayOf(0, 100, 50, 100))
    }

    private fun addPromptButton(text: String, colorHex: String, onClick: () -> Unit) {
        val button = Button(this).apply {
            this.text = text
            textSize = 10f
            setTextColor(ContextCompat.getColor(context, android.R.color.white))
            stateListAnimator = null
            val bg = GradientDrawable().apply {
                setColor(android.graphics.Color.parseColor(colorHex))
                cornerRadius = 20f
            }
            background = bg
            val lp = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                LinearLayout.LayoutParams.WRAP_CONTENT
            ).apply {
                setMargins(8, 4, 8, 4)
            }
            layoutParams = lp
            setPadding(12, 8, 12, 8)
            setOnClickListener { onClick() }
        }
        promptOptionsContainer.addView(button)
    }

    private fun hidePrompt() {
        if (promptOverlay.visibility != View.VISIBLE) return
        promptOverlay.animate()
            .alpha(0f)
            .setDuration(FADE_MS)
            .withEndAction { promptOverlay.visibility = View.GONE }
            .start()
    }

    private fun respondToPermission(requestId: String, decision: String) {
        hidePrompt()
        vibrate(50)

        coroutineScope.launch {
            try {
                withContext(Dispatchers.IO) {
                    val jsonBody = JSONObject().apply {
                        put("request_id", requestId)
                        put("decision", decision)
                    }
                    RelayClient.httpRequest(
                        method = "POST",
                        path = "/api/permission/respond",
                        body = jsonBody.toString(),
                        headers = mapOf("Content-Type" to "application/json")
                    )
                }
                Log.d(TAG, "Permission response sent via relay")
            } catch (e: Exception) {
                Log.e(TAG, "Error sending permission response", e)
            }
        }
    }

    private fun respondToPrompt(optionNum: Int) {
        hidePrompt()
        vibrate(50)

        coroutineScope.launch {
            try {
                withContext(Dispatchers.IO) {
                    val jsonBody = JSONObject().apply {
                        put("option", optionNum)
                    }
                    RelayClient.httpRequest(
                        method = "POST",
                        path = "/api/prompt/respond",
                        body = jsonBody.toString(),
                        headers = mapOf("Content-Type" to "application/json")
                    )
                }
                Log.d(TAG, "Prompt response sent via relay")
            } catch (e: Exception) {
                Log.e(TAG, "Error sending prompt response", e)
            }
        }
    }

    // --- Recording ---

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

    private fun toggleRecording() {
        if (isRecording) {
            stopRecordingAndSend()
        } else {
            startRecording()
        }
    }

    private fun startRecording() {
        try {
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
            updateUIState()
            Log.d(TAG, "Recording started: ${audioFile?.absolutePath}")
        } catch (e: Exception) {
            Log.e(TAG, "Failed to start recording", e)
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
        updateUIState()
    }

    private fun sendRecording() {
        val file = audioFile ?: return

        coroutineScope.launch {
            try {
                val responseMode = if (voiceResponseEnabled) "audio" else "text"
                val audioBytes = withContext(Dispatchers.IO) { file.readBytes() }

                val responseBody = withContext(Dispatchers.IO) {
                    RelayClient.uploadAudio(audioBytes, responseMode)
                }

                try {
                    val json = JSONObject(responseBody)
                    currentRequestId = json.optString("request_id", "").ifEmpty { null }
                } catch (_: Exception) {}
                vibrate(50)
                file.delete()
                audioFile = null
                // Trust WebSocket for state updates - no polling needed
            } catch (e: Exception) {
                Log.e(TAG, "Send failed: ${e.message}", e)
                vibrate(longArrayOf(0, 100, 100, 100))
            }
        }
    }

    // --- Abort ---

    private fun onAbortClick() {
        Log.d(TAG, "Aborting")
        currentRequestId = null
        vibrate(50)
        // Send abort to server via relay
        coroutineScope.launch {
            try {
                withContext(Dispatchers.IO) {
                    RelayClient.httpRequest(
                        method = "POST",
                        path = "/api/abort",
                        body = ""
                    )
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error sending abort", e)
            }
        }
        updateUIState()
    }

    // --- Audio playback ---

    private fun onPauseClick() {
        mediaPlayer?.let { player ->
            if (player.isPlaying) {
                player.pause()
                pauseButton.text = "Play"
            } else {
                player.start()
                pauseButton.text = "Pause"
            }
        }
    }

    private fun onReplayClick() {
        currentAudioFile?.let { file ->
            if (file.exists()) {
                playAudioFile(file)
            }
        }
    }

    private fun onDoneClick() {
        mediaPlayer?.release()
        mediaPlayer = null
        currentAudioFile?.delete()
        currentAudioFile = null
        isPlayingAudio = false

        currentRequestId?.let { sendAck(it) }
        currentRequestId = null
        updateUIState()
    }

    private fun downloadAndPlayAudio(audioPath: String, requestId: String) {
        coroutineScope.launch {
            try {
                val audioData = withContext(Dispatchers.IO) {
                    RelayClient.downloadAudio(audioPath)
                }

                if (audioData.isNotEmpty()) {
                    val tempFile = File.createTempFile("response_", ".mp3", cacheDir)
                    tempFile.writeBytes(audioData)
                    currentAudioFile = tempFile
                    currentRequestId = requestId

                    isPlayingAudio = true
                    updateUIState()
                    playAudioFile(tempFile)
                } else {
                    Log.e(TAG, "Audio download failed: empty data")
                }
            } catch (e: Exception) {
                Log.e(TAG, "Error downloading audio", e)
            }
        }
    }

    private fun playAudioFile(file: File) {
        mediaPlayer?.release()
        mediaPlayer = MediaPlayer().apply {
            setDataSource(file.absolutePath)
            setOnCompletionListener {
                pauseButton.text = "Play"
                animateIn(doneButton)
            }
            setOnErrorListener { _, _, _ ->
                Log.e(TAG, "Audio playback error")
                true
            }
            prepare()
            start()
        }
        pauseButton.text = "Pause"
    }

    private suspend fun checkResponse(requestId: String): JSONObject? {
        return try {
            val response = RelayClient.httpRequest(
                method = "GET",
                path = "/api/response/$requestId"
            )
            if (response.optBoolean("success", false)) {
                JSONObject(response.optString("body", "{}"))
            } else null
        } catch (e: Exception) {
            Log.e(TAG, "Error checking response", e)
            null
        }
    }

    private fun sendAck(requestId: String) {
        coroutineScope.launch {
            try {
                withContext(Dispatchers.IO) {
                    RelayClient.httpRequest(
                        method = "POST",
                        path = "/api/response/$requestId/ack",
                        body = ""
                    )
                }
                Log.d(TAG, "Ack sent for $requestId")
            } catch (e: Exception) {
                Log.e(TAG, "Error sending ack", e)
            }
        }
    }

    // --- UI State ---

    private fun updateUIState() {
        val claudeStatus = wsClient.claudeState.value.status
        val isConnected = wsClient.connectionStatus.value == ConnectionStatus.CONNECTED

        // Update state indicator pill
        updateStateIndicator(claudeStatus)

        // Show thinking dots in chat
        chatAdapter.setThinking(claudeStatus == "thinking")
        if (claudeStatus == "thinking" && chatAdapter.itemCount > 0) {
            chatRecyclerView.smoothScrollToPosition(chatAdapter.itemCount - 1)
        }

        when {
            isPlayingAudio -> {
                animateOut(recordButton)
                animateOut(abortButton)
                animateIn(audioControls)
                animateOut(doneButton)
            }
            isRecording -> {
                recordButton.text = "Stop & Send"
                recordButton.setBackgroundResource(R.drawable.round_button_red)
                recordButton.isEnabled = true
                recordButton.alpha = 1f
                animateIn(recordButton)
                animateOut(abortButton)
                animateOut(audioControls)
                animateOut(doneButton)
            }
            claudeStatus == "thinking" -> {
                animateOut(recordButton)
                animateIn(abortButton)
                animateOut(audioControls)
                animateOut(doneButton)
            }
            else -> {
                // Idle state - grey out if disconnected
                recordButton.text = "Record"
                if (isConnected) {
                    recordButton.setBackgroundResource(R.drawable.round_button)
                    recordButton.isEnabled = true
                    recordButton.alpha = 1f
                } else {
                    recordButton.setBackgroundResource(R.drawable.round_button_disabled)
                    recordButton.isEnabled = false
                    recordButton.alpha = 0.6f
                }
                animateIn(recordButton)
                animateOut(abortButton)
                animateOut(audioControls)
                animateOut(doneButton)
            }
        }
    }

    private fun updateStateIndicator(claudeStatus: String) {
        val bg = GradientDrawable()
        bg.cornerRadius = 24f

        when {
            isRecording -> {
                stateIndicator.text = "RECORDING"
                bg.setColor(android.graphics.Color.parseColor("#D32F2F"))
                stateIndicator.background = bg
                if (stateIndicator.visibility != View.VISIBLE) {
                    stateIndicator.alpha = 0f
                    stateIndicator.visibility = View.VISIBLE
                    stateIndicator.animate().alpha(1f).setDuration(FADE_MS).start()
                }
            }
            claudeStatus == "thinking" -> {
                stateIndicator.text = "THINKING"
                bg.setColor(android.graphics.Color.parseColor("#F57C00"))
                stateIndicator.background = bg
                if (stateIndicator.visibility != View.VISIBLE) {
                    stateIndicator.alpha = 0f
                    stateIndicator.visibility = View.VISIBLE
                    stateIndicator.animate().alpha(1f).setDuration(FADE_MS).start()
                }
            }
            isPlayingAudio -> {
                stateIndicator.text = "SPEAKING"
                bg.setColor(android.graphics.Color.parseColor("#1976D2"))
                stateIndicator.background = bg
                if (stateIndicator.visibility != View.VISIBLE) {
                    stateIndicator.alpha = 0f
                    stateIndicator.visibility = View.VISIBLE
                    stateIndicator.animate().alpha(1f).setDuration(FADE_MS).start()
                }
            }
            else -> {
                if (stateIndicator.visibility == View.VISIBLE) {
                    stateIndicator.animate()
                        .alpha(0f)
                        .setDuration(FADE_MS)
                        .withEndAction { stateIndicator.visibility = View.GONE }
                        .start()
                }
            }
        }
    }

    // --- Animations ---

    private fun animateIn(view: View) {
        if (view.visibility == View.VISIBLE && view.alpha == 1f) return
        view.alpha = 0f
        view.scaleX = 0.8f
        view.scaleY = 0.8f
        view.visibility = View.VISIBLE
        view.animate()
            .alpha(1f)
            .scaleX(1f)
            .scaleY(1f)
            .setDuration(SCALE_MS)
            .setInterpolator(OvershootInterpolator(1.2f))
            .start()
    }

    private fun animateOut(view: View) {
        if (view.visibility == View.GONE) return
        view.animate()
            .alpha(0f)
            .scaleX(0.8f)
            .scaleY(0.8f)
            .setDuration(FADE_MS)
            .setInterpolator(AccelerateDecelerateInterpolator())
            .withEndAction { view.visibility = View.GONE }
            .start()
    }

    private fun fadeIn(view: View) {
        if (view.visibility == View.VISIBLE && view.alpha == 1f) return
        view.alpha = 0f
        view.visibility = View.VISIBLE
        view.animate().alpha(1f).setDuration(FADE_MS).start()
    }

    private fun fadeOut(view: View) {
        if (view.visibility == View.GONE) return
        view.animate()
            .alpha(0f)
            .setDuration(FADE_MS)
            .withEndAction { view.visibility = View.GONE }
            .start()
    }

    // --- Permissions ---

    private fun checkPermission(): Boolean {
        return ContextCompat.checkSelfPermission(
            this, Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == PERMISSION_REQUEST_CODE) {
            if (grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                startRecording()
            }
        }
    }

    // --- Haptics ---

    private fun vibrate(duration: Long) {
        val vibrator = getSystemService(Vibrator::class.java)
        vibrator?.vibrate(VibrationEffect.createOneShot(duration, VibrationEffect.DEFAULT_AMPLITUDE))
    }

    private fun vibrate(pattern: LongArray) {
        val vibrator = getSystemService(Vibrator::class.java)
        vibrator?.vibrate(VibrationEffect.createWaveform(pattern, -1))
    }

    // --- Lifecycle ---

    private fun cleanupRecording() {
        mediaRecorder?.release()
        mediaRecorder = null
        audioFile?.delete()
        audioFile = null
        isRecording = false
        updateUIState()
    }

    override fun onNewIntent(intent: Intent?) {
        super.onNewIntent(intent)
        val action = resolveIntentAction(
            fromPermission = intent?.getBooleanExtra("from_permission", false) == true,
            autoRecord = intent?.getBooleanExtra("auto_record", false) == true,
            hasPermission = checkPermission(),
            isRecording = isRecording,
            isPlayingAudio = isPlayingAudio,
            claudeStatus = wsClient.claudeState.value.status
        )
        when (action) {
            IntentAction.IGNORE -> {}
            IntentAction.START_RECORDING -> startRecording()
            IntentAction.STOP_AND_SEND -> stopRecordingAndSend()
            IntentAction.PAUSE_AUDIO -> onPauseClick()
            IntentAction.ABORT -> onAbortClick()
            IntentAction.NONE -> {}
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        coroutineScope.cancel()
        wsClient.destroy()
        stopRecording()
        mediaPlayer?.release()
        mediaPlayer = null
        currentAudioFile?.delete()
    }
}
