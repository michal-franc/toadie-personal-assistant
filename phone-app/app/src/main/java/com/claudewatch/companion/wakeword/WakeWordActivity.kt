package com.claudewatch.companion.wakeword

import android.app.KeyguardManager
import android.content.Context
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.View
import android.view.WindowManager
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.lifecycle.lifecycleScope
import com.claudewatch.companion.R
import com.claudewatch.companion.creature.CreatureState
import com.claudewatch.companion.creature.CreatureView
import kotlinx.coroutines.launch

class WakeWordActivity : AppCompatActivity() {

    companion object {
        private const val TAG = "WakeWordActivity"
    }

    private lateinit var creatureView: CreatureView
    private lateinit var statusText: TextView
    private lateinit var audioWave: AudioWaveView
    private lateinit var keyguardManager: KeyguardManager
    private val timeoutHandler = Handler(Looper.getMainLooper())
    private val timeoutRunnable = Runnable { finish() }
    private var pendingFinish = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        keyguardManager = getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager

        // Show over lock screen and turn screen on
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
            // Request to dismiss keyguard (user still needs to unlock for secure lock screens)
            keyguardManager.requestDismissKeyguard(this, null)
        }
        window.addFlags(
            WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON or
            @Suppress("DEPRECATION")
            WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
            @Suppress("DEPRECATION")
            WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON or
            @Suppress("DEPRECATION")
            WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD
        )

        setContentView(R.layout.activity_wake_word)

        creatureView = findViewById(R.id.creature_view)
        statusText = findViewById(R.id.status_text)
        audioWave = findViewById(R.id.audio_wave)

        // Tap anywhere to stop recording and send immediately
        findViewById<LinearLayout>(R.id.root_layout).setOnClickListener {
            if (WakeWordService.wakeWordState.value == WakeWordState.RECORDING) {
                Log.i(TAG, "Tap-to-stop: user tapped overlay during recording")
                WakeWordService.requestStopRecording()
            }
        }

        // Safety timeout
        timeoutHandler.postDelayed(timeoutRunnable, 35_000L)

        lifecycleScope.launch {
            WakeWordService.wakeWordState.collect { state ->
                Log.i(TAG, "State changed to: $state")
                when (state) {
                    WakeWordState.RECORDING -> {
                        creatureView.setState(CreatureState.LISTENING)
                        statusText.text = getString(R.string.wake_word_listening)
                        audioWave.visibility = View.VISIBLE
                    }
                    WakeWordState.SENDING -> {
                        creatureView.setState(CreatureState.THINKING)
                        statusText.text = getString(R.string.wake_word_sending)
                        audioWave.visibility = View.GONE
                    }
                    WakeWordState.DONE -> {
                        Log.i(TAG, "DONE received, keyguardLocked=${keyguardManager.isKeyguardLocked}")
                        if (keyguardManager.isKeyguardLocked) {
                            // Device is locked - wait for unlock before finishing
                            pendingFinish = true
                            statusText.text = getString(R.string.wake_word_sending)
                        } else {
                            // MainActivity was already launched by service, just finish overlay
                            Log.i(TAG, "Finishing overlay")
                            finish()
                        }
                    }
                    WakeWordState.IDLE -> {
                        // Activity shouldn't be open during IDLE - finish as fallback
                        Log.i(TAG, "IDLE received while activity open, finishing")
                        finish()
                    }
                }
            }
        }

        // Observe amplitude for visual feedback
        lifecycleScope.launch {
            WakeWordService.amplitude.collect { amp ->
                audioWave.setAmplitude(amp)
            }
        }
    }

    override fun onResume() {
        super.onResume()
        // If we were waiting for unlock and now unlocked, finish overlay
        if (pendingFinish && !keyguardManager.isKeyguardLocked) {
            finish()
        }
    }

    override fun onDestroy() {
        timeoutHandler.removeCallbacks(timeoutRunnable)
        super.onDestroy()
    }
}
