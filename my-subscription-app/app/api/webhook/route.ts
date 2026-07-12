import { NextResponse } from 'next/server';
import Stripe from 'stripe';
import { createClient } from '@supabase/supabase-js';

const stripe = new Stripe(process.env.STRIPE_SECRET_KEY!);

const supabaseAdmin = createClient(
  process.env.NEXT_PUBLIC_SUPABASE_URL!,
  process.env.SUPABASE_SERVICE_ROLE_KEY!
);

export async function POST(request: Request) {
  const body = await request.text();
  const signature = request.headers.get('stripe-signature')!;

  let event: Stripe.Event;

  try {
    event = stripe.webhooks.constructEvent(
      body,
      signature,
      process.env.STRIPE_WEBHOOK_SECRET!
    );
  } catch (err: any) {
    console.error('❌ Webhook signature verification failed:', err.message);
    return NextResponse.json({ error: `Webhook Error: ${err.message}` }, { status: 400 });
  }

  // 🔔 ประมวลผลจากทั้งฝั่ง Checkout Success หรือเมื่อทำการชำระเงินบิลเสร็จสิ้น
  if (event.type === 'checkout.session.completed' || event.type === 'invoice.payment_succeeded') {
    const session = event.data.object as any;
    
    // ดึง userId จากจุดต่างๆ ที่ Stripe สุ่มส่งมาให้
    const userId = session.client_reference_id || session.metadata?.userId || session.subscription_details?.metadata?.userId;

    let priceId = '';
    
    if (event.type === 'checkout.session.completed') {
      const lineItems = await stripe.checkout.sessions.listLineItems(session.id);
      priceId = lineItems.data[0]?.price?.id || '';
    } else if (event.type === 'invoice.payment_succeeded') {
      priceId = session.lines?.data[0]?.price?.id || '';
    }

    // ⚠️ อย่าลืมตรวจสอบให้แน่ใจว่า รหัส price_... ตรงกับบนหน้าเว็บ Stripe ของคุณจริงๆ นะครับ
    let planName = 'Unknown Plan';
    if (priceId === 'price_1TsQhyChcdvHBA4uaa3itx5H') planName = 'Basic Plan';
    if (priceId === 'price_1TsQkhChcdvHBA4uDuaUBVkr') planName = 'Premium Pro';

    if (userId) {
      const { error } = await supabaseAdmin
        .from('subscriptions')
        .upsert({
          user_id: userId,
          plan_name: planName,
          status: 'Active'
        }, { onConflict: 'user_id' });

      if (error) {
        console.error('❌ Supabase update error:', error);
        return NextResponse.json({ error: 'Database update failed' }, { status: 500 });
      }

      console.log(`✅ อัปเดตแพ็กเกจ ${planName} ให้สมาชิกรหัส ${userId} สำเร็จ!`);
    } else {
      console.warn('⚠️ Webhook สัญญาณเข้ามาแต่ไม่มีรหัสข้อมูล userId ผูกไว้');
    }
  }

  return NextResponse.json({ received: true });
}