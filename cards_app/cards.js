const tg = window.Telegram?.WebApp;
if (tg) {
    try {
        if (typeof tg.expand === 'function') tg.expand();
        if (typeof tg.ready === 'function') tg.ready();
        if (typeof tg.setHeaderColor === 'function') tg.setHeaderColor('#000000');
        if (typeof tg.setBackgroundColor === 'function') tg.setBackgroundColor('#000000');
    } catch (e) {
        console.log("TG WebApp init warning", e);
    }
}

// Get user ID from Telegram WebApp, URL params, or generate unique localStorage ID
function getEffectiveUserId() {
    try {
        if (tg?.initDataUnsafe?.user?.id) {
            return tg.initDataUnsafe.user.id;
        }
        const urlParams = new URLSearchParams(window.location.search);
        const paramId = urlParams.get('tg_id');
        if (paramId && !isNaN(parseInt(paramId, 10))) {
            return parseInt(paramId, 10);
        }
        let localId = null;
        try {
            localId = localStorage.getItem('funko_cards_uid');
            if (!localId) {
                localId = Math.floor(Math.random() * 89999999) + 10000000;
                localStorage.setItem('funko_cards_uid', localId);
            }
        } catch (storageErr) {
            localId = Math.floor(Math.random() * 89999999) + 10000000;
        }
        return parseInt(localId, 10);
    } catch (err) {
        return Math.floor(Math.random() * 89999999) + 10000000;
    }
}

const effectiveTgId = getEffectiveUserId();

// User state
let userData = {
    telegram_id: effectiveTgId,
    first_name: tg?.initDataUnsafe?.user?.first_name || 'Игрок',
    username: tg?.initDataUnsafe?.user?.username || 'player',
    packs_count: 3,
    last_daily_pack: null,
    completed_tasks: [],
    ref_code: 'ref_' + effectiveTgId
};

let userCards = {}; 
let isOpening = false;
let dailyTimerInterval = null;

// 7 Series Configurations
const RARITY_ORDER = { common: 0, rare: 1, epic: 2, legendary: 3 };

const SERIES_CONFIG = [
    {
        slug: 'breaking_bad',
        name: 'Breaking Bad',
        theme: 'breaking_bad',
        cards: [
            { index: 1, name: 'Walter White',  rarity: 'legendary', img: '/cards/images/card_breaking_bad_1.png' },
            { index: 2, name: 'Jesse Pinkman', rarity: 'common',   img: '/cards/images/card_breaking_bad_2.png' },
            { index: 3, name: 'Saul Goodman',  rarity: 'rare',     img: '/cards/images/card_breaking_bad_3.png' },
            { index: 4, name: 'Gustavo Fring', rarity: 'epic',     img: '/cards/images/card_breaking_bad_4.png' }
        ]
    },
    {
        slug: 'stranger_things',
        name: 'Stranger Things',
        theme: 'stranger_things',
        cards: [
            { index: 1, name: 'Steve',         rarity: 'common',    img: '/cards/images/card_stranger_things_1.png' },
            { index: 2, name: 'Mike',          rarity: 'rare',      img: '/cards/images/card_stranger_things_2.png' },
            { index: 3, name: 'Will Byers',    rarity: 'epic',      img: '/cards/images/card_stranger_things_3.png' },
            { index: 4, name: 'Demogorgon',    rarity: 'legendary', img: '/cards/images/card_stranger_things_4.png' }
        ]
    },
    {
        slug: 'resident_evil',
        name: 'Resident Evil',
        theme: 'resident_evil',
        cards: [
            { index: 1, name: 'Chainsaw Villager', rarity: 'common',    img: '/cards/images/card_residennt_evil_1.png' },
            { index: 2, name: 'Jill Valentine',    rarity: 'rare',      img: '/cards/images/card_residennt_evil_2.png' },
            { index: 3, name: 'Albert Wesker',     rarity: 'epic',      img: '/cards/images/card_residennt_evil_3.png' },
            { index: 4, name: 'Leon Kennedy',      rarity: 'legendary', img: '/cards/images/card_residennt_evil_4.png' }
        ]
    },
    {
        slug: 'death_note',
        name: 'Death Note',
        theme: 'death_note',
        cards: [
            { index: 1, name: 'Misa Amane',   rarity: 'common',    img: '/cards/images/card_death_note_1.png' },
            { index: 2, name: 'Ryuk',         rarity: 'rare',      img: '/cards/images/card_death_note_2.png' },
            { index: 3, name: 'L',            rarity: 'epic',      img: '/cards/images/card_death_note_3.png' },
            { index: 4, name: 'Light Yagami', rarity: 'legendary', img: '/cards/images/card_death_note_4.png' }
        ]
    },
    {
        slug: 'invincible',
        name: 'Invincible',
        theme: 'invincible',
        cards: [
            { index: 1, name: 'Atom Eve',        rarity: 'common',    img: '/cards/images/card_invincible_1.png' },
            { index: 2, name: 'Allen the Alien', rarity: 'rare',      img: '/cards/images/card_invincible_2.png' },
            { index: 3, name: 'Omni-Man',        rarity: 'epic',      img: '/cards/images/card_invincible_3.png' },
            { index: 4, name: 'Invincible',      rarity: 'legendary', img: '/cards/images/card_invincible_4.png' }
        ]
    },
    {
        slug: 'one_piece',
        name: 'One Piece',
        theme: 'one_piece',
        cards: [
            { index: 1, name: 'Nami',          rarity: 'common',    img: '/cards/images/card_one_piece_1.png' },
            { index: 2, name: 'Sanji',         rarity: 'rare',      img: '/cards/images/card_one_piece_2.png' },
            { index: 3, name: 'Roronoa Zorro', rarity: 'epic',      img: '/cards/images/card_one_piece_3.png' },
            { index: 4, name: 'Monkey D Luffy', rarity: 'legendary', img: '/cards/images/card_one_piece_4.png' }
        ]
    },
    {
        slug: 'universal',
        name: 'Universal',
        theme: 'universal',
        cards: [
            { index: 1, name: 'Funko Gold Crown', rarity: 'legendary', img: '/cards/images/card_universal_1.png' },
            { index: 2, name: 'Funko Silver',     rarity: 'epic',      img: '/cards/images/card_universal_2.png' },
            { index: 3, name: 'Funko Bronze',     rarity: 'rare',      img: '/cards/images/card_universal_3.png' },
            { index: 4, name: 'Funko Classic',    rarity: 'common',    img: '/cards/images/card_universal_4.png' }
        ]
    }
];

// DOM Elements
const openPackBtn = document.getElementById('open-pack-btn');
const packsCountBadge = document.getElementById('packs-count-badge');
const boosterPack = document.getElementById('booster-pack');
const cardStage = document.getElementById('card-stage');
const card3d = document.getElementById('card-3d');
const cardFront = document.getElementById('card-front');
const seriesContainer = document.getElementById('series-container');
const totalProgressText = document.getElementById('total-progress-text');
const refLinkInput = document.getElementById('ref-link-input');
const copyRefBtn = document.getElementById('copy-ref-btn');
const dailyGiftBtn = document.getElementById('daily-gift-btn');
const dailyTimer = document.getElementById('daily-timer');

// Init
async function initApp() {
    setupNavigation();
    setupRefLink();
    setupDailyPackButton();
    setupTasks();
    await fetchProfile();
    renderCollection();
    updateUI();
}

function updateUI() {
    packsCountBadge.textContent = userData.packs_count;
    if (userData.packs_count <= 0) {
        openPackBtn.style.opacity = '0.6';
    } else {
        openPackBtn.style.opacity = '1';
    }
    
    // Always restore booster pack visibility on UI update if not actively tearing
    if (boosterPack && !isOpening) {
        boosterPack.classList.remove('hidden', 'is-tearing', 'shaking');
        const packTop = document.getElementById('pack-top');
        const packInside = document.getElementById('pack-inside-card');
        if (packTop) packTop.classList.remove('tearing-left');
        if (packInside) packInside.classList.remove('peeking');
    }
    
    updateTaskButtons();
    renderCollection();
}

function setupNavigation() {
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-view').forEach(v => {
                v.classList.add('hidden');
                v.classList.remove('active');
            });
            
            btn.classList.add('active');
            const targetId = btn.getAttribute('data-target');
            const targetView = document.getElementById(targetId);
            if (targetView) {
                targetView.classList.remove('hidden');
                targetView.classList.add('active');
            }

            // Hide floating daily gift button on Collection & Tasks tabs
            const dailyBtn = document.getElementById('daily-gift-btn');
            if (dailyBtn) {
                if (targetId === 'view-home') {
                    dailyBtn.classList.remove('hidden');
                } else {
                    dailyBtn.classList.add('hidden');
                }
            }
        });
    });
}

let botUsername = "funkostop_bot";

function setupRefLink() {
    const link = `https://t.me/${botUsername}?start=ref_${userData.telegram_id}`;
    if (refLinkInput) refLinkInput.value = link;
    if (copyRefBtn) {
        copyRefBtn.onclick = () => {
            navigator.clipboard.writeText(refLinkInput.value);
            copyRefBtn.textContent = 'Скопировано';
            setTimeout(() => copyRefBtn.textContent = 'Копировать', 2000);
        };
    }
}

async function fetchProfile() {
    try {
        const timestamp = new Date().getTime();
        const res = await fetch(`/api/cards/profile?tg_id=${userData.telegram_id}&t=${timestamp}`, { cache: 'no-store' });
        if (res.ok) {
            const data = await res.json();
            if (typeof data.packs_count === 'number') userData.packs_count = data.packs_count;
            userData.last_daily_pack = data.last_daily_pack;
            userCards = data.user_cards || {};
            userData.completed_tasks = data.completed_tasks || [];
            userData.ref_count = data.ref_count || 0;
            if (data.bot_username) {
                botUsername = data.bot_username;
                setupRefLink();
            }
        }
    } catch (e) {
        console.log("Using default profile / fetch error", e);
    } finally {
        checkDailyTimer();
        updateUI();
    }
}

// Tasks System
function setupTasks() {
    document.querySelectorAll('.btn-task[data-task]').forEach(btn => {
        btn.addEventListener('click', async () => {
            if (btn.classList.contains('completed')) return;
            
            const taskId = btn.getAttribute('data-task');
            
            // Open official Telegram channel if tg_sub task
            if (taskId === 'tg_sub') {
                window.open('https://t.me/FunkoStop', '_blank');
            }
            
            try {
                const res = await fetch('/api/cards/tasks/claim', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ telegram_id: userData.telegram_id, task_id: taskId })
                });
                const data = await res.json();
                
                if (data.success) {
                    userData.packs_count = data.packs_count;
                    userData.completed_tasks = data.completed_tasks;
                    updateUI();
                    
                    if (window.confetti) {
                        confetti({ particleCount: 50, spread: 60, origin: { y: 0.8 } });
                    }
                } else {
                    alert(data.message || "Не удалось выполнить задание.");
                }
            } catch (e) {
                console.error("Task claim error", e);
                alert("Ошибка сети или серверов. Попробуйте еще раз.");
            }
        });
    });
}

function updateTaskButtons() {
    document.querySelectorAll('.btn-task[data-task]').forEach(btn => {
        const taskId = btn.getAttribute('data-task');
        if (userData.completed_tasks.includes(taskId)) {
            btn.textContent = 'ВЫПОЛНЕНО';
            btn.classList.add('completed');
        }
    });

    // Update referral counter tag
    const refCountTag = document.getElementById('ref-count-tag');
    if (refCountTag) {
        refCountTag.textContent = `Приглашено друзей: ${userData.ref_count || 0}`;
    }
}

// Daily Pack Claiming
function setupDailyPackButton() {
    if (!dailyGiftBtn) return;
    
    dailyGiftBtn.addEventListener('click', async () => {
        const isReady = dailyTimer.textContent === "ГОТОВО";
        if (!isReady) {
            alert(`⏳ Ежедневный подарок уже получен! Следующий забор будет доступен через ${dailyTimer.textContent}.`);
            return;
        }

        try {
            const res = await fetch('/api/cards/claim_daily', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ telegram_id: userData.telegram_id })
            });
            const data = await res.json();
            if (data.success) {
                userData.packs_count = data.packs_count;
                userData.last_daily_pack = new Date().toISOString();
                updateUI();
                startDailyCountdown(86400);
                
                if (window.confetti) {
                    confetti({ particleCount: 100, spread: 70, origin: { y: 0.5 } });
                }
            } else {
                if (data.seconds_left) {
                    startDailyCountdown(data.seconds_left);
                } else {
                    alert(data.message || "Ежедневный пак пока недоступен.");
                }
            }
        } catch (e) {
            console.error("Daily pack claim fallback", e);
            userData.packs_count++;
            userData.last_daily_pack = new Date().toISOString();
            updateUI();
            startDailyCountdown(86400);
        }
    });
}

function checkDailyTimer() {
    if (!dailyTimer) return;
    if (!userData.last_daily_pack) {
        dailyTimer.textContent = "ГОТОВО";
        dailyTimer.style.color = "#00ff88";
        dailyTimer.style.borderColor = "#00ff88";
        return;
    }

    const last = new Date(userData.last_daily_pack).getTime();
    const now = new Date().getTime();
    const diffSeconds = Math.floor((now - last) / 1000);

    if (isNaN(diffSeconds) || diffSeconds >= 86400) {
        dailyTimer.textContent = "ГОТОВО";
        dailyTimer.style.color = "#00ff88";
        dailyTimer.style.borderColor = "#00ff88";
    } else {
        startDailyCountdown(86400 - diffSeconds);
    }
}

function startDailyCountdown(secondsLeft) {
    if (dailyTimerInterval) clearInterval(dailyTimerInterval);

    dailyTimer.style.color = "#ff0022";
    dailyTimer.style.borderColor = "#ff0022";

    function tick() {
        if (secondsLeft <= 0) {
            clearInterval(dailyTimerInterval);
            dailyTimer.textContent = "ГОТОВО";
            dailyTimer.style.color = "#00ff88";
            dailyTimer.style.borderColor = "#00ff88";
            return;
        }
        const h = Math.floor(secondsLeft / 3600).toString().padStart(2, '0');
        const m = Math.floor((secondsLeft % 3600) / 60).toString().padStart(2, '0');
        const s = (secondsLeft % 60).toString().padStart(2, '0');
        dailyTimer.textContent = `${h}:${m}:${s}`;
        secondsLeft--;
    }

    tick();
    dailyTimerInterval = setInterval(tick, 1000);
}



// Roll Card
function rollRandomCard() {
    const rand = Math.random() * 100;
    let selectedRarity = 'common';
    if (rand <= 2.5) selectedRarity = 'legendary';
    else if (rand <= 12.5) selectedRarity = 'epic'; // 10% chance
    else if (rand <= 37.5) selectedRarity = 'rare'; // 25% chance
    else selectedRarity = 'common';

    const matching = [];
    SERIES_CONFIG.forEach(s => {
        s.cards.forEach(c => {
            if (c.rarity === selectedRarity) {
                matching.push({ series: s, card: c });
            }
        });
    });

    return matching[Math.floor(Math.random() * matching.length)];
}

// Pack Opening Flow — Shorts-style: card shoots from bottom of screen
let fastOpen = false;

openPackBtn.addEventListener('click', async () => {
    if (isOpening) return;
    if (userData.packs_count <= 0) {
        alert("У вас закончились паки! Заберите ежедневный пак или выполняйте задания.");
        return;
    }

    isOpening = true;
    const isFast = fastOpen;
    fastOpen = false; // reset for next time
    userData.packs_count--;
    updateUI();

    // Phase 1: Pack tearing and card peeking (No shaking)
    // Remove ALL aura classes so previous card's glow doesn't carry over
    card3d.classList.remove('flipped', 'aura-common', 'aura-rare', 'aura-epic', 'aura-legendary', 'card-fly-in');
    card3d.style.filter = ''; // also clear any inline filter
    
    const rarityBadge = document.getElementById('drop-rarity-badge');
    if (rarityBadge) {
        rarityBadge.className = 'drop-rarity-badge hidden';
    }
    
    const svetBg = document.getElementById('svet-bg');
    if (svetBg) {
        svetBg.classList.remove('svet-common', 'svet-rare', 'svet-epic', 'svet-legendary', 'show');
        svetBg.style.opacity = '0';
    }
    
    const packTop = document.getElementById('pack-top');
    const packInside = document.getElementById('pack-inside-card');

    boosterPack.classList.add('is-tearing');
    packTop.classList.add('tearing-left');
    packInside.classList.add('peeking');

    setTimeout(() => {
        // Phase 2: Pack disappears, prepare drop
        boosterPack.classList.add('hidden');

            const drop = rollRandomCard();
            const cardKey = `${drop.series.slug}_${drop.card.index}`;
            userCards[cardKey] = (userCards[cardKey] || 0) + 1;

        cardFront.innerHTML = `
            <div class="card-frame ${drop.series.theme}" style="padding: 0; border: none; background: transparent; box-shadow: none; position: relative;">
                <div style="display: none; position: absolute; inset: 0; align-items: center; justify-content: center; font-size: 1.5rem; font-weight: 900; text-align: center; color: #fff; text-shadow: 0 2px 10px #000; padding: 20px; z-index: 0;">
                    ${drop.card.name}
                </div>
                <img src="${drop.card.img}" class="full-card-image" alt="${drop.card.name}" style="position: relative; z-index: 1;" onerror="this.style.display='none'; this.previousElementSibling.style.display='flex';">
            </div>
        `;

        // Phase 3: Show card stage, card starts below screen
        cardStage.style.opacity = ''; // Reset any inline opacity from previous close
        cardStage.style.visibility = ''; // Reset visibility
        cardStage.classList.remove('hidden');
        
        // Show backcard initially, hide front
        const cardBack = card3d.querySelector('.card-back');
        const cardFrontEl = card3d.querySelector('.card-front');
        if (cardBack) { cardBack.style.display = ''; cardBack.style.opacity = '1'; }
        if (cardFrontEl) { cardFrontEl.style.display = 'none'; }
        
        card3d.style.transform = 'translateY(120vh) rotateY(0deg)';
        card3d.style.transition = 'none';
        card3d.style.opacity = '0';
        
        // Reset and prepare background elements
        const svetBg = document.getElementById('svet-bg');
        const actions = document.getElementById('opened-card-actions');
        if(svetBg) svetBg.className = '';
        if(svetBg) svetBg.style.opacity = '0';
        cardStage.classList.remove('show-backcards');
        actions.classList.add('hidden');

        // Phase 4: Card SHOOTS UP from bottom
        card3d.style.transition = 'none';
        card3d.style.transform = 'translateY(120vh) rotateY(0deg)';
        card3d.style.opacity = '0';
        
        // Force reflow synchronously to ensure start state is applied
        void card3d.offsetHeight;

        const flySpeed = isFast ? '0.6s' : '0.9s';
        card3d.style.transition = `transform ${flySpeed} cubic-bezier(0.22, 0.61, 0.36, 1), opacity 0.3s ease`;
        card3d.style.transform = 'translateY(0) rotateY(0deg)';
        card3d.style.opacity = '1';

        // Phase 5: NO AURA before flip — don't spoil the rarity!
        
        // Phase 6: Wait for tap to flip
        const btnFlipCard = document.getElementById('btn-flip-card');
        
        const handleTapToFlip = (e) => {
            if (e && e.target && e.target.closest('#opened-card-actions')) return;
            btnFlipCard.removeEventListener('click', handleTapToFlip);
            card3d.removeEventListener('click', handleTapToFlip);
            btnFlipCard.classList.add('hidden');
            
            // 3D FLIP: rotateY(0) → rotateY(-90deg) → swap → rotateY(90deg) → rotateY(0)
            const halfSpeed = 500; // Always 1s total
            card3d.style.transition = `transform ${halfSpeed}ms ease-in`;
            card3d.style.transform = 'rotateY(-90deg)';
            
            setTimeout(() => {
                // Midpoint: swap backcard → front card
                const cardBack = card3d.querySelector('.card-back');
                const cardFrontEl = card3d.querySelector('.card-front');
                if (cardBack) cardBack.style.display = 'none';
                if (cardFrontEl) { cardFrontEl.style.display = 'block'; cardFrontEl.style.opacity = '1'; }
                
                // Snap to opposite side
                card3d.style.transition = 'none';
                card3d.style.transform = 'rotateY(90deg)';
                
                // Force reflow
                void card3d.offsetWidth;
                
                // NOW apply aura (rarity is revealed)
                card3d.classList.remove('aura-common', 'aura-rare', 'aura-epic', 'aura-legendary');
                card3d.classList.add('aura-' + drop.card.rarity);
                if (drop.card.rarity === 'legendary') flashScreen();
                
                // Trigger glow here (after flip) ONLY for epic and legendary
                if(svetBg && (drop.card.rarity === 'epic' || drop.card.rarity === 'legendary')) {
                    svetBg.classList.add('svet-' + drop.card.rarity);
                    svetBg.style.transition = 'opacity 0.2s ease';
                    svetBg.style.opacity = '1';
                }
                
                if (rarityBadge) {
                    rarityBadge.textContent = drop.card.rarity;
                    rarityBadge.classList.remove('hidden');
                    void rarityBadge.offsetWidth;
                    rarityBadge.classList.add('reveal-' + drop.card.rarity);
                    rarityBadge.classList.add('show');
                }
                
                // Complete flip: rotateY 90deg → 0deg
                card3d.style.transition = `transform ${Math.round(halfSpeed * 1.2)}ms cubic-bezier(0.175, 0.885, 0.32, 1.4)`;
                card3d.style.transform = 'rotateY(0deg)';
            }, halfSpeed);

            // Confetti burst on reveal
            setTimeout(() => {
                if (window.confetti) {
                    confetti({
                        particleCount: drop.card.rarity === 'legendary' ? 200 : (drop.card.rarity === 'epic' ? 100 : 50),
                        spread: drop.card.rarity === 'legendary' ? 120 : 80,
                        origin: { y: 0.5 },
                        colors: drop.card.rarity === 'legendary' ? ['#ffc107','#ff9800','#fff'] :
                                drop.card.rarity === 'epic' ? ['#e040fb','#9c27b0','#fff'] :
                                drop.card.rarity === 'rare' ? ['#2196f3','#00bcd4','#fff'] :
                                ['#4caf50','#8bc34a','#fff']
                    });
                }

                syncOpenedCard(drop.series.slug, drop.card.index);

                // Show action buttons
                actions.classList.remove('hidden');

                const closeStageFn = (hideCompletely) => {
                    if (hideCompletely) {
                        cardStage.style.opacity = '0';
                        cardStage.style.visibility = 'hidden';
                        setTimeout(() => {
                            cardStage.classList.add('hidden');
                            cardFront.innerHTML = '';
                            
                            // Hide svet glow
                            const svetBg = document.getElementById('svet-bg');
                            if (svetBg) svetBg.classList.remove('show');
                            
                            updateUI();
                            if (typeof onComplete === 'function') onComplete();
                            cardStage.style.visibility = '';
                        }, 400);
                    }
                    card3d.style.transition = 'none';
                    
                    cardStage.classList.remove('show-backcards');
                    if(svetBg) svetBg.style.opacity = '0';
                    actions.classList.add('hidden');
                    if (hideCompletely) {
                        boosterPack.classList.remove('hidden', 'shaking', 'is-tearing');
                        packTop.classList.remove('tearing-left');
                        packInside.classList.remove('peeking');
                        isOpening = false;
                        renderCollection();
                    }
                };

                const exitBtn = document.getElementById('btn-exit-pack');
                const nextBtn = document.getElementById('btn-next-pack');
                
                // Clear old listeners
                const newExit = exitBtn.cloneNode(true);
                const newNext = nextBtn.cloneNode(true);
                exitBtn.parentNode.replaceChild(newExit, exitBtn);
                nextBtn.parentNode.replaceChild(newNext, nextBtn);
                
                newExit.addEventListener('click', (e) => {
                    if (e) e.stopPropagation();
                    actions.classList.add('hidden');
                    if (rarityBadge) rarityBadge.classList.remove('show');
                    
                    card3d.style.transition = 'transform 0.4s ease, opacity 0.4s ease';
                    card3d.style.transform = 'translateY(-120vh) rotateY(180deg)';
                    card3d.style.opacity = '0';
                    if (svetBg) {
                        svetBg.style.transition = 'opacity 0.4s ease';
                        svetBg.style.opacity = '0';
                    }
                    setTimeout(() => closeStageFn(true), 400);
                });
                
                newNext.addEventListener('click', (e) => {
                    if (e) e.stopPropagation();
                    if (userData.packs_count <= 0) {
                        alert("У вас больше нет паков!");
                        return;
                    }
                    actions.classList.add('hidden');
                    if (rarityBadge) rarityBadge.classList.remove('show');
                    
                    card3d.style.transition = 'transform 0.4s ease, opacity 0.4s ease';
                    card3d.style.transform = 'translateY(120vh) rotateY(180deg)';
                    card3d.style.opacity = '0';
                    if (svetBg) {
                        svetBg.style.transition = 'opacity 0.4s ease';
                        svetBg.style.opacity = '0';
                    }
                    setTimeout(() => {
                        closeStageFn(false);
                        isOpening = false;
                        fastOpen = true; // Set flag for next open
                        document.getElementById('open-pack-btn').click();
                    }, 400);
                });

            }, isFast ? 200 : 350); // Small wait after flip starts before confetti
        };

        // Allow tapping either the button or the card itself to flip
        setTimeout(() => {
            btnFlipCard.classList.remove('hidden');
            btnFlipCard.style.animation = 'fadeInSlideUp 0.4s ease forwards';
            btnFlipCard.addEventListener('click', handleTapToFlip);
            card3d.addEventListener('click', handleTapToFlip);
        }, isFast ? 400 : 800);
        
        // Removed svetBg trigger from here so it happens on flip

    }, isFast ? 50 : 1500); // end of Phase 1 (Tearing duration)

});

function flashScreen() {
    const flash = document.createElement('div');
    flash.style.cssText = 'position:fixed;inset:0;background:#fff;z-index:9999;pointer-events:none;animation:flashAnim 0.5s ease forwards';
    const style = document.createElement('style');
    style.textContent = '@keyframes flashAnim{0%{opacity:0.9}100%{opacity:0}}';
    document.head.appendChild(style);
    document.body.appendChild(flash);
    setTimeout(() => { flash.remove(); style.remove(); }, 600);
}

function svetnFlash() {
    const svetBg = document.getElementById('svet-bg');
    if (svetBg) {
        svetBg.classList.add('show');
    }
}

async function syncOpenedCard(seriesSlug, cardIndex) {
    try {
        await fetch('/api/cards/open', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                telegram_id: userData.telegram_id,
                series_slug: seriesSlug,
                card_index: cardIndex
            })
        });
    } catch (e) {
        console.log("Card opened offline/simulated");
    }
}

// Render Collection
const MANAGER_URL = 'https://t.me/Funko_Stop';

function openLightbox(card, series, isCollected) {
    const lb = document.getElementById('card-lightbox');
    const img = document.getElementById('lightbox-img');
    const name = document.getElementById('lightbox-name');
    const rarity = document.getElementById('lightbox-rarity');
    const seriesEl = document.getElementById('lightbox-series');

    if (!isCollected) {
        img.src = card.img;
        img.style.filter = 'brightness(0) saturate(0) contrast(1.2)';
        name.textContent = '???';
    } else {
        img.src = card.img;
        img.style.filter = 'none';
        name.textContent = card.name;
    }

    const rarityLabels = { common: 'Common', rare: 'Rare', epic: 'Epic', legendary: 'Legendary' };
    rarity.textContent = rarityLabels[card.rarity] || card.rarity;
    rarity.className = `card-lightbox-rarity lightbox-rarity-${card.rarity}`;
    seriesEl.textContent = series.name;

    lb.classList.remove('hidden');
}

function closeLightbox() {
    document.getElementById('card-lightbox').classList.add('hidden');
}

function setupLightboxClose() {
    document.getElementById('lightbox-close').addEventListener('click', closeLightbox);
    document.getElementById('lightbox-close-btn').addEventListener('click', closeLightbox);
}

function renderCollection() {
    seriesContainer.innerHTML = '';
    let totalCollected = 0;
    
    // Calculate total first
    SERIES_CONFIG.forEach(series => {
        series.cards.forEach(card => {
            const cardKey = `${series.slug}_${card.index}`;
            if (userCards[cardKey] > 0) totalCollected++;
        });
    });

    // Update global header stats
    const totalCards = SERIES_CONFIG.length * 4;
    const missing = totalCards - totalCollected;
    let totalDupes = 0;
    Object.values(userCards).forEach(count => {
        if (count > 1) totalDupes += (count - 1);
    });

    // Update global header stats in DOM
    const elTotal = document.getElementById('stat-total');
    if (elTotal) elTotal.textContent = totalCards;
    const elCollected = document.getElementById('stat-collected');
    if (elCollected) elCollected.textContent = totalCollected;
    const elMissing = document.getElementById('stat-missing');
    if (elMissing) elMissing.textContent = missing;
    const elDupes = document.getElementById('stat-dupes');
    if (elDupes) elDupes.textContent = totalDupes;

    SERIES_CONFIG.forEach(series => {
        let seriesCollectedCount = 0;
        series.cards.forEach(c => { if ((userCards[`${series.slug}_${c.index}`] || 0) > 0) seriesCollectedCount++; });
        const isFull = seriesCollectedCount === 4;
        const progressPct = Math.round((seriesCollectedCount / 4) * 100);

        const seriesBlock = document.createElement('div');
        seriesBlock.className = 'series-card-block';

        // Title row
        const titleRow = document.createElement('div');
        titleRow.className = 'series-title-row';
        titleRow.innerHTML = `
            <div class="series-name">${series.name}</div>
            <div class="series-progress">${seriesCollectedCount} / 4</div>
        `;
        seriesBlock.appendChild(titleRow);

        // Card grid (4 columns) — sorted common → rare → epic → legendary
        const grid = document.createElement('div');
        grid.className = 'cards-grid-view';

        const sortedCards = [...series.cards].sort((a, b) => 
            RARITY_ORDER[a.rarity] - RARITY_ORDER[b.rarity]
        );

        sortedCards.forEach(card => {
            const cardKey = `${series.slug}_${card.index}`;
            const count = userCards[cardKey] || 0;
            const isCollected = count > 0;

            const cardWrap = document.createElement('div');
            cardWrap.className = `coll-card rarity-border-${card.rarity} ${isCollected ? 'collected' : 'locked'}`;
            cardWrap.style.cursor = 'pointer';

            const imgContainer = document.createElement('div');
            imgContainer.className = 'coll-card-img-wrap';
            imgContainer.innerHTML = `
                <img src="${card.img}" 
                     class="coll-card-img ${isCollected ? '' : 'silhouette'}" 
                     alt="${card.name}"
                     onerror="this.src=''; this.style.display='none';">
                ${!isCollected ? '<div class="coll-lock-icon">🔒</div>' : ''}
                ${count > 1 ? `<div class="coll-dupe-badge">x${count}</div>` : ''}
            `;

            const label = document.createElement('div');
            label.className = 'coll-card-label';
            label.innerHTML = `
                <span class="coll-card-name ${isCollected ? '' : 'locked-name'}">${isCollected ? card.name : '???'}</span>
                <span class="coll-rarity-dot rarity-dot-${card.rarity}"></span>
            `;

            cardWrap.appendChild(imgContainer);
            cardWrap.appendChild(label);

            // Click to preview
            cardWrap.addEventListener('click', () => openLightbox(card, series, isCollected));

            grid.appendChild(cardWrap);
        });

        seriesBlock.appendChild(grid);

        // Progress bar
        const progressWrap = document.createElement('div');
        progressWrap.className = 'series-progress-wrap';
        progressWrap.innerHTML = `
            <div class="series-progress-bar-bg">
                <div class="series-progress-bar-fill ${isFull ? 'full' : ''}" style="width:${progressPct}%"></div>
            </div>
        `;
        seriesBlock.appendChild(progressWrap);

        // Prize banner when series is complete
        if (isFull) {
            const prizeBanner = document.createElement('div');
            prizeBanner.className = 'series-prize-banner';
            prizeBanner.innerHTML = `
                <div class="series-prize-text">🏆 Коллекция собрана!<br>Напишите менеджеру за призом!</div>
                <button class="series-prize-btn" onclick="window.open('${MANAGER_URL}','_blank')">Написать →</button>
            `;
            seriesBlock.appendChild(prizeBanner);
        }

        seriesContainer.appendChild(seriesBlock);
    });
}


function setupTestButton() {
    const addTestPacksBtn = document.getElementById('add-test-packs-btn');
    if (addTestPacksBtn) {
        addTestPacksBtn.addEventListener('click', async () => {
            userData.packs_count += 10;
            updateUI();
            if (window.confetti) {
                confetti({ particleCount: 40, spread: 60, origin: { y: 0.7 } });
            }
            try {
                const res = await fetch('/api/cards/give_test_packs', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ telegram_id: userData.telegram_id })
                });
                const data = await res.json();
                if (data.success) {
                    userData.packs_count = data.packs_count;
                    updateUI();
                }
            } catch (e) {
                console.log("Local test pack addition");
            }
        });
    }

    const resetTestDailyBtn = document.getElementById('reset-test-daily-btn');
    if (resetTestDailyBtn) {
        resetTestDailyBtn.addEventListener('click', async () => {
            userData.last_daily_pack = null;
            if (dailyTimerInterval) clearInterval(dailyTimerInterval);
            checkDailyTimer();
            try {
                await fetch('/api/cards/reset_daily_test', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ telegram_id: userData.telegram_id })
                });
            } catch (e) {}
        });
    }
}

function initApp() {
    setupNavigation();
    setupRefLink();
    setupTasks();
    setupDailyPackButton();
    setupTestButton();
    setupLightboxClose();
    fetchProfile();
}

initApp();
