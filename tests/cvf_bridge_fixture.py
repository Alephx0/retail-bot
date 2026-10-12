"""Synthetic ACIC exchange: provider answers and retailer session tokens differ."""
WRAPPER_BRIDGE = '''<script>
function postChallengeEvent(eventId,payload){parent.postMessage(JSON.stringify({eventId,payload}),'*')}
</script>'''
BRIDGE_SCRIPT = '''<script>
window.fixtureAnswers=[];
window.acic={serviceEndpoint:location.origin+'/aaut/verify/',
 clientRequest:{clientReferenceIdValue:'cvf'},
 currentAAmationResult:{sessionTokenValue:'fixture-acic-session',actionTypeValue:'ARKOSE_LEVEL_1'},
 sendArnoldHttpRequest:()=>{},handleIframeMessage:()=>{}};
addEventListener('message',async e=>{
 let message;try{message=JSON.parse(e.data)}catch{return}
 if(message.eventId!=='aa-challenge-complete')return;
 fixtureAnswers.push(message.payload.sessionToken);
 const result=window.fixtureAmazonVerify?await window.fixtureAmazonVerify(message.payload):
    {actionType:'PASS',sessionToken:'fixture-amazon-session'};
 if(result.actionType!=='PASS')return;
 document.querySelector('#cvf_aamation_response_token').value=result.sessionToken;
 document.querySelector('#cvf-aamation-challenge-form').requestSubmit();
});
</script>'''
