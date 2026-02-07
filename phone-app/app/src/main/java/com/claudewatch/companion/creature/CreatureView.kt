package com.claudewatch.companion.creature

import android.animation.ValueAnimator
import android.content.Context
import android.graphics.*
import android.util.AttributeSet
import android.view.View
import android.view.animation.AccelerateDecelerateInterpolator
import android.view.animation.LinearInterpolator
import com.claudewatch.companion.R
import kotlin.math.cos
import kotlin.math.sin

class CreatureView @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0
) : View(context, attrs, defStyleAttr) {

    private var currentState = CreatureState.IDLE
    private var animationProgress = 0f
    private var breathingProgress = 0f
    private var blinkProgress = 0f
    private var thinkingBubbleProgress = 0f

    // Smooth transition properties (interpolated between states)
    private var hornAngle = 0f
    private var targetHornAngle = 0f
    private var pupilOffsetX = 0f
    private var pupilOffsetY = 0f
    private var targetPupilOffsetX = 0f
    private var targetPupilOffsetY = 0f
    private var eyeClosure = 0f
    private var targetEyeClosure = 0f
    private var bodyGrayAmount = 0f
    private var targetBodyGrayAmount = 0f

    private var stateTransitionAnimator: ValueAnimator? = null

    // Mood system — orthogonal to state, affects glow/particles/horns
    private var currentMood = CreatureMood.NEUTRAL
    private var moodGlowColor = Color.TRANSPARENT
    private var moodHornBias = 0f  // positive = perked, negative = droopy
    private var moodTransitionAnimator: ValueAnimator? = null

    // Particle system
    private val particles = mutableListOf<Particle>()
    private var lastFrameTime = System.nanoTime()
    private var particleSpawnAccumulator = 0f

    enum class ParticleType { SPARKLE, BUBBLE, SWEAT, RING, STAR }

    data class Particle(
        var x: Float, var y: Float,
        var vx: Float, var vy: Float,
        var life: Float,
        var size: Float,
        var color: Int,
        var type: ParticleType
    )

    // Paints
    private val bodyPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
    }

    private val bodyGradient: Shader
        get() {
            val centerX = width / 2f
            val centerY = height * 0.5f
            val radius = maxOf(minOf(width, height) * 0.35f, 1f)
            val gradientRadius = maxOf(radius * 1.5f, 1f)
            return RadialGradient(
                centerX, centerY - radius * 0.3f,
                gradientRadius,
                intArrayOf(
                    context.getColor(R.color.creature_green_light),
                    context.getColor(R.color.creature_green_dark)
                ),
                floatArrayOf(0.3f, 1f),
                Shader.TileMode.CLAMP
            )
        }

    private val hornPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
    }

    private val furPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
    }

    private val eyePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        style = Paint.Style.FILL
    }

    private val pupilPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.BLACK
        style = Paint.Style.FILL
    }

    private val irisPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
    }

    private val highlightPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        style = Paint.Style.FILL
    }

    private val eyeShadowPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(40, 0, 0, 0)
        style = Paint.Style.FILL
    }

    private val eyeGlowPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
    }

    private val shadowPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
    }

    private val glowPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
    }

    private val particlePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
    }

    private val mouthPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.BLACK
        style = Paint.Style.STROKE
        strokeWidth = 4f
        strokeCap = Paint.Cap.ROUND
    }

    private val nosePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
    }

    private val noseHighlightPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.argb(60, 255, 255, 255)
        style = Paint.Style.FILL
    }

    private val wrinklePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        strokeWidth = 1.5f
        strokeCap = Paint.Cap.ROUND
        color = Color.argb(30, 0, 0, 0)
    }

    private val bubblePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        alpha = 180
        style = Paint.Style.FILL
    }

    private val zzzPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = Color.WHITE
        textSize = 32f
        typeface = Typeface.DEFAULT_BOLD
    }

    // Animators
    private val breathingAnimator = ValueAnimator.ofFloat(0f, 1f).apply {
        duration = 3000
        repeatCount = ValueAnimator.INFINITE
        repeatMode = ValueAnimator.REVERSE
        interpolator = AccelerateDecelerateInterpolator()
        addUpdateListener { animator ->
            breathingProgress = animator.animatedValue as Float
            invalidate()
        }
    }

    private val blinkAnimator = ValueAnimator.ofFloat(0f, 1f, 0f).apply {
        duration = 200
        interpolator = LinearInterpolator()
        addUpdateListener { animator ->
            blinkProgress = animator.animatedValue as Float
            invalidate()
        }
    }

    private val thinkingAnimator = ValueAnimator.ofFloat(0f, 1f).apply {
        duration = 2000
        repeatCount = ValueAnimator.INFINITE
        interpolator = LinearInterpolator()
        addUpdateListener { animator ->
            thinkingBubbleProgress = animator.animatedValue as Float
            invalidate()
        }
    }

    private val mainAnimator = ValueAnimator.ofFloat(0f, 1f).apply {
        duration = 1000
        repeatCount = ValueAnimator.INFINITE
        repeatMode = ValueAnimator.REVERSE
        interpolator = AccelerateDecelerateInterpolator()
        addUpdateListener { animator ->
            animationProgress = animator.animatedValue as Float
            invalidate()
        }
    }

    init {
        breathingAnimator.start()
        mainAnimator.start()
        startRandomBlinks()
    }

    private fun startRandomBlinks() {
        postDelayed({
            if (currentState != CreatureState.SLEEPING && currentState != CreatureState.THINKING) {
                blinkAnimator.start()
            }
            startRandomBlinks()
        }, (2000..5000).random().toLong())
    }

    fun setState(state: CreatureState) {
        if (currentState == state) return
        currentState = state

        when (state) {
            CreatureState.THINKING -> thinkingAnimator.start()
            else -> thinkingAnimator.cancel()
        }

        // Set targets based on new state
        targetHornAngle = when (state) {
            CreatureState.LISTENING -> -15f
            CreatureState.OFFLINE -> 25f
            CreatureState.SLEEPING -> 15f
            else -> 0f
        }
        targetPupilOffsetX = 0f
        targetPupilOffsetY = when (state) {
            CreatureState.LISTENING -> -2f
            CreatureState.OFFLINE -> 4f
            else -> 0f
        }
        targetEyeClosure = when (state) {
            CreatureState.SLEEPING, CreatureState.THINKING -> 1f
            else -> 0f
        }
        targetBodyGrayAmount = when (state) {
            CreatureState.OFFLINE -> 1f
            else -> 0f
        }

        // Animate from current values to targets
        val startHorn = hornAngle
        val startPupilX = pupilOffsetX
        val startPupilY = pupilOffsetY
        val startEyeClosure = eyeClosure
        val startGray = bodyGrayAmount

        stateTransitionAnimator?.cancel()
        stateTransitionAnimator = ValueAnimator.ofFloat(0f, 1f).apply {
            duration = 350
            interpolator = AccelerateDecelerateInterpolator()
            addUpdateListener { animator ->
                val t = animator.animatedValue as Float
                hornAngle = startHorn + (targetHornAngle - startHorn) * t
                pupilOffsetX = startPupilX + (targetPupilOffsetX - startPupilX) * t
                pupilOffsetY = startPupilY + (targetPupilOffsetY - startPupilY) * t
                eyeClosure = startEyeClosure + (targetEyeClosure - startEyeClosure) * t
                bodyGrayAmount = startGray + (targetBodyGrayAmount - startGray) * t
                invalidate()
            }
            start()
        }

        // Clear particles on state change so new state starts fresh
        particles.clear()
    }

    fun setMood(mood: CreatureMood) {
        if (currentMood == mood) return
        currentMood = mood

        val targetGlow = when (mood) {
            CreatureMood.NEUTRAL -> Color.TRANSPARENT
            CreatureMood.HAPPY -> Color.argb(60, 0xFF, 0xD7, 0x00)     // gold
            CreatureMood.CURIOUS -> Color.argb(50, 0x00, 0x99, 0xFF)   // blue
            CreatureMood.FOCUSED -> Color.argb(40, 0xFF, 0xFF, 0xFF)   // white
            CreatureMood.PROUD -> Color.argb(55, 0xFF, 0x8C, 0x00)     // orange
            CreatureMood.CONFUSED -> Color.argb(50, 0x9C, 0x27, 0xB0)  // purple
            CreatureMood.PLAYFUL -> Color.argb(50, 0xFF, 0x69, 0xB4)   // pink
        }
        val targetHornBias = when (mood) {
            CreatureMood.HAPPY, CreatureMood.CURIOUS, CreatureMood.PLAYFUL -> -8f  // perked up
            CreatureMood.CONFUSED -> 10f  // droopy
            CreatureMood.PROUD -> -5f
            else -> 0f
        }

        val startGlow = moodGlowColor
        val startHornBias = moodHornBias
        val evaluator = android.animation.ArgbEvaluator()

        moodTransitionAnimator?.cancel()
        moodTransitionAnimator = ValueAnimator.ofFloat(0f, 1f).apply {
            duration = 500
            interpolator = AccelerateDecelerateInterpolator()
            addUpdateListener { animator ->
                val t = animator.animatedValue as Float
                moodGlowColor = evaluator.evaluate(t, startGlow, targetGlow) as Int
                moodHornBias = startHornBias + (targetHornBias - startHornBias) * t
                invalidate()
            }
            start()
        }
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)

        val centerX = width / 2f
        val centerY = height * 0.5f
        val baseRadius = minOf(width, height) * 0.3f

        // Apply breathing/bouncing effect
        val breathScale = 1f + breathingProgress * 0.03f
        val bounceOffset = when (currentState) {
            CreatureState.SPEAKING -> sin(animationProgress * Math.PI.toFloat() * 2) * 10f
            else -> 0f
        }

        // Draw shadow and glow beneath body (not affected by body transform)
        drawShadowAndGlow(canvas, centerX, centerY, baseRadius, breathScale, bounceOffset)

        canvas.save()
        canvas.translate(0f, -bounceOffset)
        canvas.scale(breathScale, breathScale, centerX, centerY)

        // Draw body (wider troll shape)
        drawBody(canvas, centerX, centerY, baseRadius)

        // Draw horns
        drawHorns(canvas, centerX, centerY, baseRadius)

        // Draw fur tufts on cheeks
        drawFur(canvas, centerX, centerY, baseRadius)

        // Draw eyes (larger, yellow)
        drawEyes(canvas, centerX, centerY, baseRadius)

        // Draw nose
        drawNose(canvas, centerX, centerY, baseRadius)

        // Draw mouth (wider grin)
        drawMouth(canvas, centerX, centerY, baseRadius)

        canvas.restore()

        // Draw effects (not affected by body transform)
        when (currentState) {
            CreatureState.THINKING -> drawThinkingBubbles(canvas, centerX, centerY, baseRadius)
            CreatureState.SLEEPING -> drawZzz(canvas, centerX, centerY, baseRadius)
            else -> {}
        }

        // Update and draw particles
        updateParticles(centerX, centerY, baseRadius)
        drawParticles(canvas)

        // Keep animation ticking for particles
        if (particles.isNotEmpty() || currentState != CreatureState.OFFLINE) {
            postInvalidateOnAnimation()
        }
    }

    private fun drawShadowAndGlow(canvas: Canvas, cx: Float, cy: Float, radius: Float, breathScale: Float, bounceOffset: Float) {
        val shadowCx = cx
        val shadowCy = cy + radius * 1.15f - bounceOffset * 0.3f
        val shadowRadiusX = radius * 0.8f * breathScale
        val shadowRadiusY = radius * 0.15f

        // Dark shadow oval
        shadowPaint.shader = RadialGradient(
            shadowCx, shadowCy,
            maxOf(shadowRadiusX, 1f),
            intArrayOf(Color.argb(50, 0, 0, 0), Color.TRANSPARENT),
            floatArrayOf(0.3f, 1f),
            Shader.TileMode.CLAMP
        )
        canvas.save()
        canvas.scale(1f, shadowRadiusY / maxOf(shadowRadiusX, 1f), shadowCx, shadowCy)
        canvas.drawCircle(shadowCx, shadowCy, shadowRadiusX, shadowPaint)
        canvas.restore()

        // State-colored glow
        val glowColor = when (currentState) {
            CreatureState.IDLE -> context.getColor(R.color.creature_glow_idle)
            CreatureState.THINKING -> {
                val alpha = (30 + 20 * sin(thinkingBubbleProgress * Math.PI.toFloat() * 2)).toInt()
                Color.argb(alpha, 0, 0x99, 0xFF)
            }
            CreatureState.SPEAKING -> context.getColor(R.color.creature_glow_green)
            CreatureState.LISTENING -> context.getColor(R.color.creature_glow_listening)
            CreatureState.SLEEPING -> context.getColor(R.color.creature_glow_purple)
            CreatureState.OFFLINE -> Color.TRANSPARENT
        }

        if (glowColor != Color.TRANSPARENT) {
            val glowRadius = radius * 0.9f * breathScale
            glowPaint.shader = RadialGradient(
                shadowCx, shadowCy,
                maxOf(glowRadius, 1f),
                intArrayOf(glowColor, Color.TRANSPARENT),
                floatArrayOf(0f, 1f),
                Shader.TileMode.CLAMP
            )
            canvas.save()
            canvas.scale(1f, 0.4f, shadowCx, shadowCy)
            canvas.drawCircle(shadowCx, shadowCy, glowRadius, glowPaint)
            canvas.restore()
        }

        // Mood glow overlay (additive, layered on top of state glow)
        if (moodGlowColor != Color.TRANSPARENT) {
            val moodGlowRadius = radius * 1.1f * breathScale
            glowPaint.shader = RadialGradient(
                shadowCx, shadowCy,
                maxOf(moodGlowRadius, 1f),
                intArrayOf(moodGlowColor, Color.TRANSPARENT),
                floatArrayOf(0f, 1f),
                Shader.TileMode.CLAMP
            )
            canvas.save()
            canvas.scale(1f, 0.4f, shadowCx, shadowCy)
            canvas.drawCircle(shadowCx, shadowCy, moodGlowRadius, glowPaint)
            canvas.restore()
        }
    }

    private fun drawBody(canvas: Canvas, cx: Float, cy: Float, radius: Float) {
        bodyPaint.shader = bodyGradient

        val wobble = if (currentState == CreatureState.SPEAKING) {
            sin(animationProgress * Math.PI.toFloat() * 4) * 5f
        } else 0f

        // Wider, squatter troll shape: wider rx, shorter ry
        val path = Path()
        val rx = radius * 1.15f + wobble
        val ry = radius * 0.95f

        // Upper head (rounded top)
        path.addOval(
            cx - rx,
            cy - ry * 0.85f,
            cx + rx,
            cy + ry * 1.05f,
            Path.Direction.CW
        )
        canvas.drawPath(path, bodyPaint)

        // Prominent jaw/chin bump
        val jawPath = Path()
        jawPath.addOval(
            cx - rx * 0.7f,
            cy + ry * 0.55f,
            cx + rx * 0.7f,
            cy + ry * 1.2f,
            Path.Direction.CW
        )
        canvas.drawPath(jawPath, bodyPaint)

        // Subtle wrinkle lines on face
        val wrinkleY = cy - radius * 0.02f
        canvas.drawLine(cx - radius * 0.15f, wrinkleY, cx + radius * 0.15f, wrinkleY, wrinklePaint)
        canvas.drawLine(cx - radius * 0.1f, wrinkleY + radius * 0.06f, cx + radius * 0.1f, wrinkleY + radius * 0.06f, wrinklePaint)
    }

    private fun drawHorns(canvas: Canvas, cx: Float, cy: Float, radius: Float) {
        val hornWidth = radius * 0.15f
        val hornHeight = radius * 0.55f
        val hornSpacing = radius * 0.4f

        for (side in listOf(-1f, 1f)) {
            val hornBaseX = cx + side * hornSpacing
            val hornBaseY = cy - radius * 0.75f

            canvas.save()
            canvas.rotate(side * (hornAngle + moodHornBias), hornBaseX, hornBaseY)

            val hornPath = Path()
            // Triangular/conical horn
            hornPath.moveTo(hornBaseX - hornWidth, hornBaseY)
            hornPath.lineTo(hornBaseX + side * hornWidth * 0.3f, hornBaseY - hornHeight)
            hornPath.lineTo(hornBaseX + hornWidth, hornBaseY)
            hornPath.close()

            // Gradient fill: bone base to pink inner
            hornPaint.shader = LinearGradient(
                hornBaseX, hornBaseY,
                hornBaseX, hornBaseY - hornHeight,
                intArrayOf(
                    context.getColor(R.color.creature_horn_base),
                    context.getColor(R.color.creature_horn_inner)
                ),
                floatArrayOf(0f, 1f),
                Shader.TileMode.CLAMP
            )
            canvas.drawPath(hornPath, hornPaint)

            canvas.restore()
        }
    }

    private fun drawFur(canvas: Canvas, cx: Float, cy: Float, radius: Float) {
        val furColor = context.getColor(R.color.creature_fur)
        furPaint.color = furColor
        furPaint.alpha = when (currentState) {
            CreatureState.SLEEPING -> 140
            else -> 180
        }
        furPaint.strokeWidth = radius * 0.04f

        // Fur animation
        val furOffset = when (currentState) {
            CreatureState.SPEAKING -> sin(animationProgress * Math.PI.toFloat() * 3) * 3f
            CreatureState.SLEEPING -> 2f  // droops
            else -> 0f
        }

        for (side in listOf(-1f, 1f)) {
            val baseX = cx + side * radius * 0.95f
            val baseY = cy - radius * 0.05f

            // Multiple small curved fur strokes
            for (i in 0..4) {
                val offsetY = i * radius * 0.08f
                val path = Path()
                path.moveTo(baseX, baseY + offsetY)
                path.quadTo(
                    baseX + side * radius * 0.2f,
                    baseY + offsetY + radius * 0.05f + furOffset,
                    baseX + side * radius * 0.15f,
                    baseY + offsetY + radius * 0.12f + furOffset
                )
                canvas.drawPath(path, furPaint)
            }
        }
    }

    private fun drawEyes(canvas: Canvas, cx: Float, cy: Float, radius: Float) {
        val eyeRadius = radius * 0.22f  // Larger eyes for troll
        val eyeSpacing = radius * 0.38f
        val eyeY = cy - radius * 0.18f

        // Combine interpolated state closure with blink
        val effectiveClosure = maxOf(eyeClosure, blinkProgress)
        val eyeScaleY = 1f - effectiveClosure * 0.9f

        // Draw each eye (left then right)
        for ((idx, side) in listOf(-1f, 1f).withIndex()) {
            val ex = cx + side * eyeSpacing

            // Subtle yellow glow behind eyes when active
            if (currentState != CreatureState.OFFLINE && currentState != CreatureState.SLEEPING) {
                eyeGlowPaint.shader = RadialGradient(
                    ex, eyeY,
                    maxOf(eyeRadius * 1.6f, 1f),
                    intArrayOf(Color.argb(40, 0xE8, 0xB4, 0x00), Color.TRANSPARENT),
                    floatArrayOf(0.2f, 1f),
                    Shader.TileMode.CLAMP
                )
                canvas.drawCircle(ex, eyeY, eyeRadius * 1.6f, eyeGlowPaint)
            }

            canvas.save()
            canvas.scale(1f, eyeScaleY, ex, eyeY)

            // Eye white with subtle gradient for depth
            eyePaint.shader = RadialGradient(
                ex, eyeY,
                maxOf(eyeRadius, 1f),
                intArrayOf(Color.WHITE, Color.argb(255, 230, 230, 230)),
                floatArrayOf(0.5f, 1f),
                Shader.TileMode.CLAMP
            )
            canvas.drawCircle(ex, eyeY, eyeRadius, eyePaint)
            eyePaint.shader = null

            // Lower shadow crescent for 3D depth
            val shadowRect = RectF(
                ex - eyeRadius * 0.8f,
                eyeY + eyeRadius * 0.2f,
                ex + eyeRadius * 0.8f,
                eyeY + eyeRadius * 0.9f
            )
            eyeShadowPaint.shader = RadialGradient(
                ex, eyeY + eyeRadius * 0.7f,
                maxOf(eyeRadius * 0.8f, 1f),
                intArrayOf(Color.argb(40, 0, 0, 0), Color.TRANSPARENT),
                floatArrayOf(0f, 1f),
                Shader.TileMode.CLAMP
            )
            canvas.drawOval(shadowRect, eyeShadowPaint)
            eyeShadowPaint.shader = null

            // Iris and pupil only when eyes are open enough
            if (effectiveClosure < 0.5f) {
                val irisRadius = eyeRadius * 0.6f
                val pupilDilate = when (currentState) {
                    CreatureState.LISTENING -> 1.15f
                    CreatureState.THINKING -> 0.85f
                    else -> 1f
                }
                val pupilRadius = irisRadius * 0.55f * pupilDilate

                // Slightly asymmetric pupil positions for character
                val asymOffset = if (idx == 0) -1f else 1.5f
                val irisX = ex + pupilOffsetX + asymOffset
                val irisY = eyeY + pupilOffsetY

                // Iris with yellow/amber gradient
                irisPaint.shader = RadialGradient(
                    irisX, irisY,
                    maxOf(irisRadius, 1f),
                    intArrayOf(
                        context.getColor(R.color.creature_iris),
                        context.getColor(R.color.creature_iris_dark)
                    ),
                    floatArrayOf(0.3f, 1f),
                    Shader.TileMode.CLAMP
                )
                canvas.drawCircle(irisX, irisY, irisRadius, irisPaint)

                // Pupil
                canvas.drawCircle(irisX, irisY, pupilRadius, pupilPaint)

                // Specular highlight (top-left, fixed position relative to eye)
                val hlRadius = eyeRadius * 0.2f
                canvas.drawCircle(
                    ex - eyeRadius * 0.25f,
                    eyeY - eyeRadius * 0.25f,
                    hlRadius,
                    highlightPaint
                )
            }

            canvas.restore()
        }
    }

    private fun drawNose(canvas: Canvas, cx: Float, cy: Float, radius: Float) {
        val noseX = cx
        val noseY = cy + radius * 0.12f
        val noseRx = radius * 0.08f
        val noseRy = radius * 0.06f

        // Slightly darker green nose
        nosePaint.shader = RadialGradient(
            noseX, noseY,
            maxOf(noseRx * 1.5f, 1f),
            intArrayOf(
                context.getColor(R.color.creature_green_dark),
                context.getColor(R.color.creature_fur)
            ),
            floatArrayOf(0.3f, 1f),
            Shader.TileMode.CLAMP
        )
        canvas.drawOval(
            noseX - noseRx, noseY - noseRy,
            noseX + noseRx, noseY + noseRy,
            nosePaint
        )

        // Small highlight
        canvas.drawOval(
            noseX - noseRx * 0.4f, noseY - noseRy * 0.6f,
            noseX + noseRx * 0.2f, noseY - noseRy * 0.1f,
            noseHighlightPaint
        )
    }

    private fun drawMouth(canvas: Canvas, cx: Float, cy: Float, radius: Float) {
        val mouthY = cy + radius * 0.38f
        val mouthWidth = radius * 0.6f  // Much wider grin

        val path = Path()
        when (currentState) {
            CreatureState.SPEAKING -> {
                // Wide open mouth animation
                val openAmount = sin(animationProgress * Math.PI.toFloat() * 3) * 0.5f + 0.5f
                path.addOval(
                    cx - mouthWidth * 0.6f,
                    mouthY - radius * 0.12f * openAmount,
                    cx + mouthWidth * 0.6f,
                    mouthY + radius * 0.25f * openAmount,
                    Path.Direction.CW
                )
                canvas.drawPath(path, Paint(pupilPaint).apply { style = Paint.Style.FILL })
            }
            CreatureState.OFFLINE -> {
                // Sad frown
                path.moveTo(cx - mouthWidth * 0.7f, mouthY + 10f)
                path.quadTo(cx, mouthY - 12f, cx + mouthWidth * 0.7f, mouthY + 10f)
                canvas.drawPath(path, mouthPaint)
            }
            else -> {
                // Wide, friendly grin with slight underbite feel
                path.moveTo(cx - mouthWidth, mouthY)
                path.cubicTo(
                    cx - mouthWidth * 0.5f, mouthY + 25f,
                    cx + mouthWidth * 0.5f, mouthY + 25f,
                    cx + mouthWidth, mouthY
                )
                canvas.drawPath(path, mouthPaint)

                // Subtle underbite line below the grin
                val underPath = Path()
                underPath.moveTo(cx - mouthWidth * 0.5f, mouthY + 18f)
                underPath.quadTo(cx, mouthY + 22f, cx + mouthWidth * 0.5f, mouthY + 18f)
                val underPaint = Paint(mouthPaint).apply {
                    strokeWidth = 2f
                    alpha = 80
                }
                canvas.drawPath(underPath, underPaint)
            }
        }
    }

    private fun drawThinkingBubbles(canvas: Canvas, cx: Float, cy: Float, radius: Float) {
        val bubbleX = cx + radius * 1.2f
        val bubbleY = cy - radius * 0.5f

        // Three bubbles of increasing size
        val sizes = floatArrayOf(6f, 10f, 16f)
        val offsets = floatArrayOf(0f, 20f, 45f)

        for (i in sizes.indices) {
            val alpha = ((thinkingBubbleProgress + i * 0.3f) % 1f)
            bubblePaint.alpha = (180 * (1f - alpha * 0.5f)).toInt()
            canvas.drawCircle(
                bubbleX + offsets[i] * cos(alpha * Math.PI.toFloat() * 0.2f),
                bubbleY - offsets[i] - alpha * 10f,
                sizes[i],
                bubblePaint
            )
        }
    }

    private fun drawZzz(canvas: Canvas, cx: Float, cy: Float, radius: Float) {
        val startX = cx + radius * 0.8f
        val startY = cy - radius * 0.3f

        zzzPaint.alpha = 255
        canvas.drawText("z", startX, startY - animationProgress * 20f, zzzPaint)

        zzzPaint.alpha = 180
        zzzPaint.textSize = 40f
        canvas.drawText("z", startX + 15f, startY - 25f - animationProgress * 20f, zzzPaint)

        zzzPaint.alpha = 120
        zzzPaint.textSize = 48f
        canvas.drawText("Z", startX + 35f, startY - 55f - animationProgress * 20f, zzzPaint)

        zzzPaint.textSize = 32f  // Reset
    }

    // --- Particle system ---

    private fun updateParticles(cx: Float, cy: Float, radius: Float) {
        val now = System.nanoTime()
        val dt = ((now - lastFrameTime) / 1_000_000_000f).coerceIn(0f, 0.1f)
        lastFrameTime = now

        // Update existing particles
        val iter = particles.iterator()
        while (iter.hasNext()) {
            val p = iter.next()
            p.life -= dt * 0.8f
            if (p.life <= 0f) {
                iter.remove()
                continue
            }
            p.x += p.vx * dt
            p.y += p.vy * dt
            // Rings expand
            if (p.type == ParticleType.RING) {
                p.size += 40f * dt
            }
        }

        // Spawn new particles based on state
        particleSpawnAccumulator += dt
        val spawnInterval = when (currentState) {
            CreatureState.SPEAKING -> 0.08f
            CreatureState.THINKING -> 0.15f
            CreatureState.LISTENING -> 0.2f
            CreatureState.IDLE -> 0.6f
            CreatureState.SLEEPING -> 0.4f
            CreatureState.OFFLINE -> Float.MAX_VALUE
        }

        val maxParticles = 20

        while (particleSpawnAccumulator >= spawnInterval && particles.size < maxParticles) {
            particleSpawnAccumulator -= spawnInterval
            spawnParticle(cx, cy, radius)
        }
        if (particleSpawnAccumulator > spawnInterval) {
            particleSpawnAccumulator = 0f
        }
    }

    private fun spawnParticle(cx: Float, cy: Float, radius: Float) {
        val rng = { min: Float, max: Float -> min + Math.random().toFloat() * (max - min) }

        when (currentState) {
            CreatureState.SPEAKING -> {
                // Gold sparkles rising from body (matches yellow eyes)
                particles.add(Particle(
                    x = cx + rng(-radius * 0.5f, radius * 0.5f),
                    y = cy + rng(-radius * 0.3f, radius * 0.3f),
                    vx = rng(-15f, 15f),
                    vy = rng(-60f, -30f),
                    life = 1f,
                    size = rng(3f, 6f),
                    color = Color.argb(200, 0xFF, 0xD7, 0x00),
                    type = ParticleType.SPARKLE
                ))
            }
            CreatureState.THINKING -> {
                // White/blue bubbles drifting up-right from head
                particles.add(Particle(
                    x = cx + radius * 0.8f + rng(-5f, 5f),
                    y = cy - radius * 0.5f + rng(-10f, 10f),
                    vx = rng(8f, 20f),
                    vy = rng(-25f, -15f),
                    life = 1f,
                    size = rng(3f, 7f),
                    color = Color.argb(160, 0xCC, 0xDD, 0xFF),
                    type = ParticleType.BUBBLE
                ))
            }
            CreatureState.LISTENING -> {
                // Pulse rings expanding from horns (green tones)
                val side = if (Math.random() > 0.5) -1f else 1f
                particles.add(Particle(
                    x = cx + side * radius * 0.4f,
                    y = cy - radius * 0.75f,
                    vx = 0f,
                    vy = 0f,
                    life = 1f,
                    size = 5f,
                    color = Color.argb(120, 0x66, 0xBB, 0x6A),
                    type = ParticleType.RING
                ))
            }
            CreatureState.IDLE -> {
                // Sparse ambient green motes
                particles.add(Particle(
                    x = cx + rng(-radius, radius),
                    y = cy + rng(-radius * 0.5f, radius * 0.5f),
                    vx = rng(-5f, 5f),
                    vy = rng(-10f, -5f),
                    life = 1f,
                    size = rng(2f, 4f),
                    color = Color.argb(80, 0x66, 0xBB, 0x6A),
                    type = ParticleType.SPARKLE
                ))
            }
            CreatureState.SLEEPING -> {
                // Tiny slow stars
                particles.add(Particle(
                    x = cx + rng(-radius * 0.8f, radius * 0.8f),
                    y = cy - radius * rng(0.3f, 1.0f),
                    vx = rng(-3f, 3f),
                    vy = rng(-8f, -3f),
                    life = 1f,
                    size = rng(2f, 4f),
                    color = Color.argb(100, 0xFF, 0xFF, 0xFF),
                    type = ParticleType.STAR
                ))
            }
            CreatureState.OFFLINE -> { /* no particles */ }
        }
    }

    private fun drawParticles(canvas: Canvas) {
        for (p in particles) {
            val alpha = (p.life.coerceIn(0f, 1f) * Color.alpha(p.color)).toInt()
            if (alpha <= 0) continue

            particlePaint.color = Color.argb(
                alpha,
                Color.red(p.color),
                Color.green(p.color),
                Color.blue(p.color)
            )

            when (p.type) {
                ParticleType.SPARKLE -> {
                    // Diamond shape
                    val path = Path()
                    path.moveTo(p.x, p.y - p.size)
                    path.lineTo(p.x + p.size * 0.6f, p.y)
                    path.lineTo(p.x, p.y + p.size)
                    path.lineTo(p.x - p.size * 0.6f, p.y)
                    path.close()
                    canvas.drawPath(path, particlePaint)
                }
                ParticleType.BUBBLE -> {
                    canvas.drawCircle(p.x, p.y, p.size, particlePaint)
                }
                ParticleType.RING -> {
                    particlePaint.style = Paint.Style.STROKE
                    particlePaint.strokeWidth = 2f
                    canvas.drawCircle(p.x, p.y, p.size, particlePaint)
                    particlePaint.style = Paint.Style.FILL
                }
                ParticleType.STAR -> {
                    // Small 4-point star
                    val path = Path()
                    val s = p.size
                    path.moveTo(p.x, p.y - s)
                    path.lineTo(p.x + s * 0.3f, p.y - s * 0.3f)
                    path.lineTo(p.x + s, p.y)
                    path.lineTo(p.x + s * 0.3f, p.y + s * 0.3f)
                    path.lineTo(p.x, p.y + s)
                    path.lineTo(p.x - s * 0.3f, p.y + s * 0.3f)
                    path.lineTo(p.x - s, p.y)
                    path.lineTo(p.x - s * 0.3f, p.y - s * 0.3f)
                    path.close()
                    canvas.drawPath(path, particlePaint)
                }
                ParticleType.SWEAT -> {
                    canvas.drawCircle(p.x, p.y, p.size, particlePaint)
                }
            }
        }
    }

    override fun onDetachedFromWindow() {
        super.onDetachedFromWindow()
        breathingAnimator.cancel()
        blinkAnimator.cancel()
        thinkingAnimator.cancel()
        mainAnimator.cancel()
        stateTransitionAnimator?.cancel()
        moodTransitionAnimator?.cancel()
    }
}
