/** Preserve receiver contract errors separately from successfully parsed evidence. */
export type ReceiverObservation = {
  responseBytes?:number;
  returnedEvidence?:unknown;
  receiverError?:'receiver_invalid_json';
};

export function observeReceiverJson(text:string):ReceiverObservation {
  const responseBytes=Buffer.byteLength(text);
  try {return {responseBytes,returnedEvidence:JSON.parse(text)};}
  catch {return {responseBytes,receiverError:'receiver_invalid_json'};}
}

export function receiverObservationErrors(requests:ReceiverObservation[]):string[] {
  return requests.some(request=>request.receiverError==='receiver_invalid_json')?['receiver_invalid_json']:[];
}
