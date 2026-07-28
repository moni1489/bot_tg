const tg = window.Telegram?.WebApp;
if (tg) {
    tg.expand();
    tg.ready();
}

// User state
let userData = {
    telegram_id: tg?.initDataUnsafe?.user?.id || 12345678,
    first_name: tg?.initDataUnsafe?.user?.first_name || 'Игрок',
    username: tg?.initDataUnsafe?.user?.username || 'player',
    packs_count: 3,
    last_daily_pack: null,
    ref_code: 'ref_' + (tg?.initDataUnsafe?.user?.id || 12345678)
};

let userCards = {}; // { "breaking_bad_1": 2, ... }
let isOpening = false;

// 7 Series Configurations
const SERIES_CONFIG = [
    {
        slug: 'breaking_bad',
        name: 'Breaking Bad',
        theme: 'breaking_bad',
        cards: [
            { index: 1, name: 'Walter White', rarity: 'legendary', img: 'https://images.unsplash.com/photo-1534447677768-be436bb09401?w=300' },
            { index: 2, name: 'Jesse Pinkman', rarity: 'epic', img: 'https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=300' },
            { index: 3, name: 'Saul Goodman', rarity: 'rare', img: 'https://images.unsplash.com/photo-1500648767791-00dcc994a43e?w=300' },
            { index: 4, name: 'Gus Fring', rarity: 'common', img: 'https://images.unsplash.com/photo-1472099645785-5658abf4ff4e?w=300' }
        ]
    },
    {
        slug: 'marvel',
        name: 'Marvel',
        theme: 'marvel',
        cards: [
            { index: 1, name: 'Iron Man', rarity: 'legendary', img: 'https://images.unsplash.com/photo-1635863138275-d9b33299680b?w=300' },
            { index: 2, name: 'Spider-Man', rarity: 'epic', img: 'https://images.unsplash.com/photo-1604200213928-ba3cf4fc8436?w=300' },
            { index: 3, name: 'Captain America', rarity: 'rare', img: 'https://images.unsplash.com/photo-1569003339405-ea396a5a8a90?w=300' },
            { index: 4, name: 'Deadpool', rarity: 'common', img: 'https://images.unsplash.com/photo-1534809027769-b00d750a6bac?w=300' }
        ]
    },
    {
        slug: 'dc',
        name: 'DC Comics',
        theme: 'dc',
        cards: [
            { index: 1, name: 'Batman', rarity: 'legendary', img: 'https://images.unsplash.com/photo-1509198397868-475647b2a1e5?w=300' },
            { index: 2, name: 'Superman', rarity: 'epic', img: 'https://images.unsplash.com/photo-1568602471122-7832951cc4c5?w=300' },
            { index: 3, name: 'Joker', rarity: 'rare', img: 'https://images.unsplash.com/photo-1579783902614-a3fb3927b675?w=300' },
            { index: 4, name: 'Flash', rarity: 'common', img: 'https://images.unsplash.com/photo-1519085360753-af0119f7cbe7?w=300' }
        ]
    },
    {
        slug: 'death_note',
        name: 'Death Note',
        theme: 'death_note',
        cards: [
            { index: 1, name: 'Ryuk', rarity: 'legendary', img: 'https://images.unsplash.com/photo-1518709268805-4e9042af9f23?w=300' },
            { index: 2, name: 'L', rarity: 'epic', img: 'https://images.unsplash.com/photo-1539571696357-5a69c17a67c6?w=300' },
            { index: 3, name: 'Light Yagami', rarity: 'rare', img: 'https://images.unsplash.com/photo-1501196354995-cbb51c65aaea?w=300' },
            { index: 4, name: 'Misa Aane', rarity: 'common', img: 'https://images.unsplash.com/photo-1524504388940-b1c1722653e1?w=300' }
        ]
    },
    {
        slug: 'invincible',
        name: 'Invincible',
        theme: 'invincible',
        cards: [
            { index: 1, name: 'Omni-Man', rarity: 'legendary', img: 'https://images.unsplash.com/photo-1506794778202-cad84cf45f1d?w=300' },
            { index: 2, name: 'Invincible', rarity: 'epic', img: 'https://images.unsplash.com/photo-1517841905240-472988babdf9?w=300' },
            { index: 3, name: 'Atom Eve', rarity: 'rare', img: 'https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=300' },
            { index: 4, name: 'Allen', rarity: 'common', img: 'https://images.unsplash.com/photo-1522075469751-3a6694fb2f61?w=300' }
        ]
    },
    {
        slug: 'one_piece',
        name: 'One Piece',
        theme: 'one_piece',
        cards: [
            { index: 1, name: 'Luffy', rarity: 'legendary', img: 'https://images.unsplash.com/photo-1544005313-94ddf0286df2?w=300' },
            { index: 2, name: 'Zoro', rarity: 'epic', img: 'https://images.unsplash.com/photo-1506794778202-cad84cf45f1d?w=300' },
            { index: 3, name: 'Sanji', rarity: 'rare', img: 'https://images.unsplash.com/photo-1519085360753-af0119f7cbe7?w=300' },
            { index: 4, name: 'Nami', rarity: 'common', img: 'https://images.unsplash.com/photo-1517841905240-472988babdf9?w=300' }
        ]
    },
    {
        slug: 'universal',
        name: 'Universal',
        theme: 'universal',
        cards: [
            { index: 1, name: 'Funko Gold Crown', rarity: 'legendary', img: 'https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=300' },
            { index: 2, name: 'Funko Silver', rarity: 'epic', img: 'https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=300' },
            { index: 3, name: 'Funko Bronze', rarity: 'rare', img: 'https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=300' },
            { index: 4, name: 'Funko Classic', rarity: 'common', img: 'https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=300' }
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

// Init
async function initApp() {
    setupNavigation();
    setupRefLink();
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
}

function setupNavigation() {
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.tab-view').forEach(v => v.classList.add('hidden'));
            
            btn.classList.add('active');
            const targetId = btn.getAttribute('data-target');
            document.getElementById(targetId).classList.remove('hidden');
        });
    });
}

function setupRefLink() {
    const link = `https://t.me/Funko_Stop_bot?start=ref_${userData.telegram_id}`;
    if (refLinkInput) refLinkInput.value = link;
    if (copyRefBtn) {
        copyRefBtn.addEventListener('click', () => {
            navigator.clipboard.writeText(link);
            copyRefBtn.textContent = 'Скопировано!';
            setTimeout(() => copyRefBtn.textContent = 'Копировать', 2000);
        });
    }
}

// API Calls
async function fetchProfile() {
    try {
        const res = await fetch(`/api/cards/profile?tg_id=${userData.telegram_id}`);
        if (res.ok) {
            const data = await res.json();
            userData.packs_count = data.packs_count;
            userCards = data.user_cards || {};
        }
    } catch (e) {
        console.log("Using default profile");
    }
}

// Roll Card (Drop Rates: Common 60%, Rare 25%, Epic 12%, Legendary 3%)
function rollRandomCard() {
    const rand = Math.random() * 100;
    let selectedRarity = 'common';
    if (rand <= 3) selectedRarity = 'legendary';
    else if (rand <= 15) selectedRarity = 'epic';
    else if (rand <= 40) selectedRarity = 'rare';
    else selectedRarity = 'common';

    // Filter matching cards across all series
    const matching = [];
    SERIES_CONFIG.forEach(s => {
        s.cards.forEach(c => {
            if (c.rarity === selectedRarity) {
                matching.push({ series: s, card: c });
            }
        });
    });

    const chosen = matching[Math.floor(Math.random() * matching.length)];
    return chosen;
}

// Pack Opening Flow
openPackBtn.addEventListener('click', async () => {
    if (isOpening) return;
    if (userData.packs_count <= 0) {
        alert("У вас закончились паки! Выполняйте задания или приглашайте друзей.");
        return;
    }

    isOpening = true;
    userData.packs_count--;
    updateUI();

    // 1. Shake Pack
    boosterPack.classList.add('shaking');
    cardStage.classList.add('hidden');
    card3d.classList.remove('flipped');

    setTimeout(() => {
        boosterPack.classList.remove('shaking');
        boosterPack.classList.add('hidden');
        cardStage.classList.remove('hidden');

        // Roll card
        const drop = rollRandomCard();
        const cardKey = `${drop.series.slug}_${drop.card.index}`;
        userCards[cardKey] = (userCards[cardKey] || 0) + 1;

        // Render front of card
        cardFront.innerHTML = `
            <div class="card-frame ${drop.series.theme}">
                <div class="card-series-tag">${drop.series.name}</div>
                <img src="${drop.card.img}" class="card-character-image" alt="${drop.card.name}">
                <div class="card-character-name">${drop.card.name}</div>
                <div class="card-rarity-badge rarity-${drop.card.rarity}">${drop.card.rarity}</div>
            </div>
        `;

        // 2. Flip 3D Card
        setTimeout(() => {
            card3d.classList.add('flipped');

            // Fire confetti
            if (window.confetti) {
                confetti({
                    particleCount: drop.card.rarity === 'legendary' ? 120 : 60,
                    spread: 70,
                    origin: { y: 0.6 }
                });
            }

            // Sync with backend
            syncOpenedCard(drop.series.slug, drop.card.index);

            // Re-enable button after 2.5s
            setTimeout(() => {
                isOpening = false;
                boosterPack.classList.remove('hidden');
                cardStage.classList.add('hidden');
                card3d.classList.remove('flipped');
                renderCollection();
            }, 3000);

        }, 400);

    }, 1200);
});

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

// Render Collection (7 Series)
function renderCollection() {
    seriesContainer.innerHTML = '';
    let totalCollected = 0;

    SERIES_CONFIG.forEach(series => {
        let seriesCollectedCount = 0;

        const seriesBlock = document.createElement('div');
        seriesBlock.className = 'series-card-block';

        const titleRow = document.createElement('div');
        titleRow.className = 'series-title-row';

        const grid = document.createElement('div');
        grid.className = 'cards-grid-4';

        series.cards.forEach(card => {
            const cardKey = `${series.slug}_${card.index}`;
            const count = userCards[cardKey] || 0;
            const isCollected = count > 0;

            if (isCollected) {
                seriesCollectedCount++;
                totalCollected++;
            }

            const slot = document.createElement('div');
            slot.className = `slot-card ${isCollected ? 'collected' : ''}`;

            if (isCollected) {
                slot.innerHTML = `
                    <img src="${card.img}" alt="${card.name}">
                    ${count > 1 ? `<span class="duplicate-count-tag">x${count}</span>` : ''}
                `;
            } else {
                slot.innerHTML = `
                    <div style="font-size:1.2rem; opacity:0.3;">🔒</div>
                    <div style="font-size:0.55rem; color:#888; text-align:center; margin-top:4px;">${card.name}</div>
                `;
            }

            grid.appendChild(slot);
        });

        titleRow.innerHTML = `
            <div class="series-name">${series.name}</div>
            <div class="series-progress">${seriesCollectedCount} / 4 ${seriesCollectedCount === 4 ? '✅' : ''}</div>
        `;

        seriesBlock.appendChild(titleRow);
        seriesBlock.appendChild(grid);
        seriesContainer.appendChild(seriesBlock);
    });

    totalProgressText.textContent = `Собрано: ${totalCollected} / 28`;
}

initApp();
